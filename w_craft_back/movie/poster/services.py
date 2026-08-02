"""Service layer for project poster generation.

Keeps controller-side code thin. The mock generator at the bottom is a
deliberate stub: real AI workers will replace ``complete_generation_mock``
without touching the views.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional
import uuid

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from w_craft_back.movie.poster.errors import (
    IdempotencyConflict,
    PosterConcurrencyLimit,
    PosterQuotaExceeded,
)
from w_craft_back.movie.poster.generation_guard import (
    daily_quota,
    daily_quota_per_user,
    ensure_provider_circuit_closed,
    job_lease_seconds,
    max_active_jobs,
    max_active_jobs_per_user,
    max_output_bytes,
    max_output_pixels,
    quota_window_start,
)
from w_craft_back.movie.poster.models import (
    POSTER_FORMAT_DIMENSIONS,
    PosterGenerationJob,
    PosterJobOperation,
    PosterJobStatus,
    PosterVariant,
    ProjectPoster,
    ProjectPosterStatus,
)
from w_craft_back.movie.project.dashboard_models import ProjectAsset
from w_craft_back.movie.project.models import Project
from w_craft_back.storage_gateway import (
    NormalizedImage,
    StorageGatewayError,
    delete_storage_key,
    normalize_image_bytes,
    signed_url_for_asset,
    signed_url_for_file,
    store_normalized_image,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _variant_image_url(variant: PosterVariant, request=None) -> Optional[str]:
    project = variant.project if request is not None else None
    return (
        signed_url_for_file(variant.image, request, project=project)
        or signed_url_for_asset(
            storage_key=None,
            legacy_url=variant.image_url,
            request=request,
            project=project,
        )
    )


def _variant_thumbnail_url(variant: PosterVariant, request=None) -> Optional[str]:
    project = variant.project if request is not None else None
    return (
        signed_url_for_file(variant.thumbnail, request, project=project)
        or signed_url_for_asset(
            storage_key=None,
            legacy_url=variant.thumbnail_url,
            request=request,
            project=project,
        )
        or _variant_image_url(variant, request)
    )


# --------------------------------------------------------------------------- #
# Read-side serialization (camelCase to match the rest of the API)
# --------------------------------------------------------------------------- #

def serialize_variant(variant: PosterVariant, request=None) -> dict[str, Any]:
    return {
        "id": variant.id,
        "jobId": variant.job_id,
        "projectId": variant.project_id,
        "imageUrl": _variant_image_url(variant, request),
        "thumbnailUrl": _variant_thumbnail_url(variant, request),
        "variantIndex": variant.variant_index,
        "width": variant.width,
        "height": variant.height,
        "isSelected": variant.is_selected,
        "createdAt": variant.created_at.isoformat() if variant.created_at else None,
    }


def serialize_job(job: PosterGenerationJob, request=None) -> dict[str, Any]:
    return {
        "id": job.id,
        "operation": job.operation,
        "posterId": job.poster_id,
        "projectId": job.project_id,
        "status": job.status,
        "progress": job.progress,
        "attempts": job.attempts,
        "requestedModel": job.requested_model or None,
        "retryOf": job.retry_of_id,
        "cancellationRequestedAt": (
            job.cancellation_requested_at.isoformat()
            if job.cancellation_requested_at
            else None
        ),
        "prompt": job.prompt,
        "style": job.style,
        "format": job.format,
        "aspectRatio": job.aspect_ratio,
        "width": job.width,
        "height": job.height,
        "errorMessage": job.error_message or None,
        "errorCode": job.error_code or None,
        "errorHttpStatus": job.error_http_status,
        "createdAt": job.created_at.isoformat() if job.created_at else None,
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "heartbeatAt": (
            job.heartbeat_at.isoformat() if job.heartbeat_at else None
        ),
        "providerStartedAt": (
            job.provider_started_at.isoformat()
            if job.provider_started_at
            else None
        ),
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
    }


def serialize_job_history(job: PosterGenerationJob, request=None) -> dict[str, Any]:
    data = serialize_job(job, request)
    data.pop("prompt", None)
    return data


def serialize_poster(
    poster: ProjectPoster,
    *,
    recent_variants: Optional[list[PosterVariant]] = None,
    request=None,
) -> dict[str, Any]:
    selected = poster.selected_variant
    selected_payload = (
        serialize_variant(selected, request)
        if selected and not selected.is_deleted
        else None
    )
    return {
        "id": poster.id,
        "projectId": poster.project_id,
        "status": poster.status,
        "selectedVariant": selected_payload,
        "recentVariants": [
            serialize_variant(v, request) for v in (recent_variants or [])
        ],
        "updatedAt": poster.updated_at.isoformat() if poster.updated_at else None,
    }


class InvalidProviderImage(ValueError):
    """Provider output failed bounded image validation before storage."""

# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #


def get_or_create_project_poster(project: Project, user: User) -> ProjectPoster:
    poster, _ = ProjectPoster.objects.get_or_create(
        project=project,
        defaults={"user": user},
    )
    return poster


def list_recent_variants(
    project: Project,
    *,
    limit: int = 8,
) -> list[PosterVariant]:
    return list(
        PosterVariant.objects.filter(project=project, is_deleted=False)
        .order_by("-created_at")[:limit]
    )


def resolve_reference_asset(
    project: Project,
    asset_id: Optional[int],
) -> Optional[ProjectAsset]:
    """Look up a previously uploaded reference asset, scoped to the project so
    one tenant can't point at another's file."""
    if not asset_id:
        return None
    asset = ProjectAsset.objects.filter(
        project=project,
        pk=asset_id,
        asset_type__in={"image", "reference", "storyboard"},
    ).first()
    if asset is None:
        return None
    mime_type = str((asset.metadata or {}).get("mime_type") or "")
    return asset if mime_type in {"image/jpeg", "image/png", "image/webp"} else None


def _expire_stale_jobs(user: User, now) -> None:
    """Recover only expired processing leases; queued work remains durable."""
    stale_jobs = PosterGenerationJob.objects.select_for_update().filter(
        user=user,
        status=PosterJobStatus.PROCESSING,
        lease_expires_at__lte=now,
    )
    for job in stale_jobs:
        if job.provider_started_at is not None:
            job.status = PosterJobStatus.FAILED
            job.error_message = "Poster provider outcome is unknown after lease expiry"
            job.error_code = "PROVIDER_OUTCOME_UNKNOWN"
            job.error_http_status = 503
            job.completed_at = now
        elif job.attempts >= job.max_attempts:
            job.status = PosterJobStatus.FAILED
            job.error_message = "Poster generation retry limit reached"
            job.error_code = "MAX_ATTEMPTS_EXCEEDED"
            job.error_http_status = 503
            job.completed_at = now
        else:
            job.status = PosterJobStatus.QUEUED
            job.progress = 0
            job.error_message = ""
            job.error_code = ""
            job.error_http_status = None
        job.lease_token = None
        job.lease_expires_at = None
        job.save()


def enqueue_generation_job(
    *,
    project: Project,
    user: User,
    prompt: str,
    style: str,
    format: str,
    operation: str = PosterJobOperation.GENERATE,
    idempotency_key: str = "",
    request_hash: str = "",
    requested_model: str = "",
    reference_storage_key: str = "",
    reference_mime_type: str = "",
    reference_image_url: str = "",
    reference_asset: Optional[ProjectAsset] = None,
    source_variant: Optional[PosterVariant] = None,
) -> tuple[ProjectPoster, PosterGenerationJob, bool]:
    """Create one guarded job or replay the job for the same request key."""
    aspect, width, height = POSTER_FORMAT_DIMENSIONS[format]

    with transaction.atomic():
        Project.objects.select_for_update().get(pk=project.pk)
        User.objects.select_for_update().get(pk=user.pk)
        now = timezone.now()
        _expire_stale_jobs(user, now)

        if idempotency_key:
            existing = (
                PosterGenerationJob.objects
                .select_related("poster")
                .filter(
                    project=project,
                    user=user,
                    operation=operation,
                    idempotency_key=idempotency_key,
                )
                .first()
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used with another payload"
                    )
                return existing.poster, existing, False

        active_statuses = [PosterJobStatus.QUEUED, PosterJobStatus.PROCESSING]
        active_count = PosterGenerationJob.objects.filter(
            project=project,
            user=user,
            status__in=active_statuses,
        ).count()
        if active_count >= max_active_jobs():
            raise PosterConcurrencyLimit(
                "Another poster generation request is already active"
            )

        user_active_count = PosterGenerationJob.objects.filter(
            user=user,
            status__in=active_statuses,
        ).count()
        if user_active_count >= max_active_jobs_per_user():
            raise PosterConcurrencyLimit(
                "Too many poster generation requests are active for this user"
            )

        user_recent_count = PosterGenerationJob.objects.filter(
            user=user,
            created_at__gte=quota_window_start(),
        ).count()
        if user_recent_count >= daily_quota_per_user():
            raise PosterQuotaExceeded(
                "Poster generation account quota is exhausted"
            )

        recent_count = PosterGenerationJob.objects.filter(
            project=project,
            user=user,
            created_at__gte=quota_window_start(),
        ).count()
        if recent_count >= daily_quota():
            raise PosterQuotaExceeded("Poster generation quota is exhausted")

        poster = get_or_create_project_poster(project, user)
        job = PosterGenerationJob.objects.create(
            poster=poster,
            project=project,
            user=user,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            requested_model=requested_model or "",
            reference_storage_key=reference_storage_key or "",
            reference_mime_type=reference_mime_type or "",
            prompt=prompt,
            style=style,
            format=format,
            aspect_ratio=aspect,
            width=width,
            height=height,
            reference_image_url=reference_image_url or "",
            reference_asset=reference_asset,
            source_variant=source_variant,
            status=PosterJobStatus.QUEUED,
        )

        if poster.status in (
            ProjectPosterStatus.EMPTY,
            ProjectPosterStatus.FAILED,
        ):
            poster.status = ProjectPosterStatus.GENERATING
            poster.save(update_fields=["status", "updated_at"])

    return poster, job, True


def select_variant(
    *,
    project: Project,
    variant: PosterVariant,
) -> ProjectPoster:
    """Mark ``variant`` as the project's selected poster.

    Asserts the variant belongs to this project and is not soft-deleted; the
    caller should already have done access checks.
    """
    if variant.project_id != project.id:
        raise ValueError("variant does not belong to project")
    if variant.is_deleted:
        raise ValueError("variant is deleted")

    with transaction.atomic():
        poster = get_or_create_project_poster(project, variant.user)

        # Single-row update, then a single-row update — matches how Drizzle/
        # Sequelize peers do this and keeps the partial index hot.
        PosterVariant.objects.filter(
            poster=poster, is_selected=True
        ).update(is_selected=False)
        PosterVariant.objects.filter(pk=variant.pk).update(is_selected=True)
        variant.refresh_from_db(fields=["is_selected"])

        poster.selected_variant = variant
        poster.status = ProjectPosterStatus.READY
        poster.save(update_fields=["selected_variant", "status", "updated_at"])

    return poster


def soft_delete_variant(*, variant: PosterVariant) -> ProjectPoster:
    """Soft-delete ``variant``. If it was the selected one, fall back to the
    next non-deleted variant or clear the selection."""
    poster = variant.poster

    with transaction.atomic():
        PosterVariant.objects.filter(pk=variant.pk).update(
            is_deleted=True, is_selected=False, updated_at=timezone.now()
        )

        if poster.selected_variant_id == variant.id:
            fallback = (
                PosterVariant.objects
                .filter(poster=poster, is_deleted=False)
                .exclude(pk=variant.pk)
                .order_by("-created_at")
                .first()
            )
            if fallback is not None:
                PosterVariant.objects.filter(pk=fallback.pk).update(is_selected=True)
                poster.selected_variant = fallback
                poster.status = ProjectPosterStatus.READY
            else:
                poster.selected_variant = None
                # Empty if no jobs ever produced anything else; otherwise keep
                # READY so the UI doesn't flash to the empty state when other
                # variants still exist (rare race — defensive).
                remaining = PosterVariant.objects.filter(
                    poster=poster, is_deleted=False
                ).exists()
                poster.status = (
                    ProjectPosterStatus.READY
                    if remaining
                    else ProjectPosterStatus.EMPTY
                )
            poster.save(
                update_fields=["selected_variant", "status", "updated_at"]
            )

    return poster


def mark_generation_processing(
    job: PosterGenerationJob,
    *,
    provider_name: str,
    model_name: str,
) -> Optional[PosterGenerationJob]:
    """Claim a queued job with a fenced lease."""
    with transaction.atomic():
        locked = PosterGenerationJob.objects.select_for_update().get(pk=job.pk)
        if locked.status != PosterJobStatus.QUEUED:
            return None
        if locked.attempts >= locked.max_attempts:
            locked.status = PosterJobStatus.FAILED
            locked.error_code = "MAX_ATTEMPTS_EXCEEDED"
            locked.error_message = "Poster generation retry limit reached"
            locked.completed_at = timezone.now()
            locked.save()
            return None
        now = timezone.now()
        locked.status = PosterJobStatus.PROCESSING
        locked.progress = 10
        locked.attempts += 1
        locked.lease_token = uuid.uuid4()
        locked.started_at = locked.started_at or now
        locked.heartbeat_at = now
        locked.model_provider = provider_name[:64]
        locked.model_name = model_name[:128]
        locked.lease_expires_at = now + timedelta(seconds=job_lease_seconds())
        locked.save(
            update_fields=[
                "status",
                "progress",
                "attempts",
                "lease_token",
                "started_at",
                "heartbeat_at",
                "model_provider",
                "model_name",
                "lease_expires_at",
                "updated_at",
            ]
        )
        return locked


def start_generation_provider_call(
    job: PosterGenerationJob,
    *,
    provider_key: str,
    provider_name: str,
    model_name: str,
) -> Optional[PosterGenerationJob]:
    """Reserve the provider circuit and mark the call started atomically."""
    with transaction.atomic():
        locked = PosterGenerationJob.objects.select_for_update().get(pk=job.pk)
        if (
            locked.status != PosterJobStatus.PROCESSING
            or locked.lease_token is None
            or locked.lease_token != job.lease_token
        ):
            return None

        ensure_provider_circuit_closed(provider_key)
        now = timezone.now()
        locked.provider_started_at = now
        locked.heartbeat_at = now
        locked.lease_expires_at = now + timedelta(seconds=job_lease_seconds())
        locked.model_provider = provider_name[:64]
        locked.model_name = model_name[:128]
        locked.save(
            update_fields=[
                "provider_started_at",
                "heartbeat_at",
                "lease_expires_at",
                "model_provider",
                "model_name",
                "updated_at",
            ]
        )
        return locked


def prepare_generation_images(
    image_bytes_list: list[bytes],
) -> list[NormalizedImage]:
    """Validate provider output independently from durable storage writes."""
    prepared = []
    for image_bytes in image_bytes_list:
        try:
            prepared.append(
                normalize_image_bytes(
                    image_bytes,
                    max_bytes=max_output_bytes(),
                    max_pixels=max_output_pixels(),
                )
            )
        except StorageGatewayError as exc:
            raise InvalidProviderImage(str(exc)) from exc
    return prepared



def complete_generation(
    job: PosterGenerationJob,
    image_bytes_list: list[bytes],
    *,
    prepared_images: Optional[list[NormalizedImage]] = None,
) -> list[PosterVariant]:
    """Normalize/store provider results, then commit metadata exactly once."""

    normalized_images = (
        prepared_images
        if prepared_images is not None
        else prepare_generation_images(image_bytes_list)
    )
    prepared = []
    persisted = False
    try:
        for image in normalized_images:
            prepared.append(
                store_normalized_image(
                    image,
                    namespace=f"projects/{job.project_id}/posters/variants",
                )
            )

        with transaction.atomic():
            locked = (
                PosterGenerationJob.objects
                .select_for_update(of=("self",))
                .select_related("poster", "project", "user")
                .get(pk=job.pk)
            )
            if locked.status == PosterJobStatus.COMPLETED:
                return list(locked.variants.order_by("variant_index"))
            if (
                locked.status != PosterJobStatus.PROCESSING
                or locked.lease_token is None
                or locked.lease_token != job.lease_token
            ):
                return []

            created: list[PosterVariant] = []
            for index, stored in enumerate(prepared):
                variant = PosterVariant(
                    job=locked,
                    poster=locked.poster,
                    project=locked.project,
                    user=locked.user,
                    variant_index=index,
                    width=stored.width,
                    height=stored.height,
                    file_size_bytes=stored.size_bytes,
                    mime_type=stored.mime_type,
                )
                variant.image.name = stored.storage_key
                variant.save()
                created.append(variant)

            locked.status = PosterJobStatus.COMPLETED
            locked.progress = 100
            locked.completed_at = timezone.now()
            locked.heartbeat_at = locked.completed_at
            locked.lease_token = None
            locked.lease_expires_at = None
            locked.save(
                update_fields=[
                    "status",
                    "progress",
                    "completed_at",
                    "heartbeat_at",
                    "lease_token",
                    "lease_expires_at",
                    "updated_at",
                ]
            )

            poster = locked.poster
            poster.status = ProjectPosterStatus.READY
            if created:
                first = created[0]
                PosterVariant.objects.filter(
                    poster=poster,
                    is_selected=True,
                ).update(is_selected=False)
                PosterVariant.objects.filter(pk=first.pk).update(is_selected=True)
                first.is_selected = True
                poster.selected_variant = first
            poster.save(update_fields=["selected_variant", "status", "updated_at"])
            persisted = True
            return created
    finally:
        if not persisted:
            for stored in prepared:
                delete_storage_key(stored.storage_key)


def fail_generation(
    job: PosterGenerationJob,
    *,
    error_message: str,
    error_code: str = "",
    error_http_status: int | None = None,
) -> None:
    """Persist a provider failure while preserving an existing good poster."""
    with transaction.atomic():
        locked = (
            PosterGenerationJob.objects
            .select_for_update()
            .select_related("poster")
            .get(pk=job.pk)
        )
        if locked.status not in (
            PosterJobStatus.QUEUED,
            PosterJobStatus.PROCESSING,
        ):
            return
        if (
            locked.status == PosterJobStatus.PROCESSING
            and locked.lease_token != job.lease_token
        ):
            return
        locked.status = PosterJobStatus.FAILED
        locked.error_message = error_message
        locked.error_code = error_code or ""
        locked.error_http_status = error_http_status
        locked.completed_at = timezone.now()
        locked.lease_token = None
        locked.lease_expires_at = None
        locked.save(
            update_fields=[
                "status",
                "error_message",
                "error_code",
                "error_http_status",
                "completed_at",
                "lease_token",
                "lease_expires_at",
                "updated_at",
            ]
        )

        poster = locked.poster
        if poster.selected_variant_id is None:
            poster.status = ProjectPosterStatus.FAILED
            poster.save(update_fields=["status", "updated_at"])


# --------------------------------------------------------------------------- #
# Mock worker — replace with the real AI pipeline when it lands.
# --------------------------------------------------------------------------- #

# Placeholder image data: a 1x1 transparent PNG. The real worker will write
# the model's output bytes; until then we just persist a deterministic blob so
# the rest of the pipeline (selection, listing, FE rendering) can be exercised.
_PLACEHOLDER_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAY"
    "AAjCB0C8AAAAASUVORK5CYII="
)


def complete_generation_mock(
    job: PosterGenerationJob,
    *,
    variant_count: int = 4,
) -> list[PosterVariant]:
    """Complete a guarded job with deterministic local placeholder images."""
    import base64

    placeholder_bytes = base64.b64decode(_PLACEHOLDER_PNG_BASE64)
    claimed = mark_generation_processing(
        job,
        provider_name="mock",
        model_name="mock-poster-provider",
    )
    if claimed is None:
        return []
    return complete_generation(
        claimed,
        [placeholder_bytes for _ in range(variant_count)],
    )


def fail_generation_mock(
    job: PosterGenerationJob,
    *,
    error_message: str,
    error_code: str = "",
    error_http_status: int | None = None,
) -> None:
    """Backward-compatible alias for tests that simulate worker failure."""
    fail_generation(
        job,
        error_message=error_message,
        error_code=error_code,
        error_http_status=error_http_status,
    )
