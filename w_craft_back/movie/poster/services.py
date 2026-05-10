"""Service layer for project poster generation.

Keeps controller-side code thin. The mock generator at the bottom is a
deliberate stub: real AI workers will replace ``complete_generation_mock``
without touching the views.
"""

from __future__ import annotations

from typing import Any, Optional

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from w_craft_back.movie.poster.models import (
    POSTER_FORMAT_DIMENSIONS,
    PosterGenerationJob,
    PosterJobStatus,
    PosterVariant,
    ProjectPoster,
    ProjectPosterStatus,
)
from w_craft_back.movie.project.dashboard_models import ProjectAsset
from w_craft_back.movie.project.models import Project


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _absolute_url(request, image_field) -> Optional[str]:
    """Mirror of services._absolute_url for the project module — kept local
    so this module doesn't reach into project.services for a private helper."""
    if not image_field:
        return None
    try:
        url = image_field.url
    except (ValueError, AttributeError):
        return None
    if request is None:
        return url
    return request.build_absolute_uri(url)


def _variant_image_url(variant: PosterVariant, request=None) -> Optional[str]:
    return (
        _absolute_url(request, variant.image)
        or variant.image_url
        or None
    )


def _variant_thumbnail_url(variant: PosterVariant, request=None) -> Optional[str]:
    return (
        _absolute_url(request, variant.thumbnail)
        or variant.thumbnail_url
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
        "posterId": job.poster_id,
        "projectId": job.project_id,
        "status": job.status,
        "prompt": job.prompt,
        "style": job.style,
        "format": job.format,
        "aspectRatio": job.aspect_ratio,
        "width": job.width,
        "height": job.height,
        "errorMessage": job.error_message or None,
        "errorCode": job.error_code or None,
        "createdAt": job.created_at.isoformat() if job.created_at else None,
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
    }


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
    return ProjectAsset.objects.filter(project=project, pk=asset_id).first()


def enqueue_generation_job(
    *,
    project: Project,
    user: User,
    prompt: str,
    style: str,
    format: str,
    reference_image_url: str = "",
    reference_asset: Optional[ProjectAsset] = None,
) -> tuple[ProjectPoster, PosterGenerationJob]:
    """Create the job in QUEUED state and flip the poster to GENERATING.

    Wrapped in a single transaction so the poster row, the job row, and the
    status change are persisted atomically — a worker that races us will
    always see a coherent (poster, job) pair.
    """
    aspect, width, height = POSTER_FORMAT_DIMENSIONS[format]

    with transaction.atomic():
        poster = get_or_create_project_poster(project, user)

        job = PosterGenerationJob.objects.create(
            poster=poster,
            project=project,
            user=user,
            prompt=prompt,
            style=style,
            format=format,
            aspect_ratio=aspect,
            width=width,
            height=height,
            reference_image_url=reference_image_url or "",
            reference_asset=reference_asset,
            status=PosterJobStatus.QUEUED,
        )

        # Don't downgrade a "ready" poster optimistically — only flip from
        # empty/failed to generating so a subsequent failed retry doesn't hide
        # an already-selected good variant.
        if poster.status in (
            ProjectPosterStatus.EMPTY,
            ProjectPosterStatus.FAILED,
        ):
            poster.status = ProjectPosterStatus.GENERATING
            poster.save(update_fields=["status", "updated_at"])
        elif poster.status == ProjectPosterStatus.READY:
            # Keep status READY but track that a new generation is in flight
            # via the job rows themselves.
            pass

    return poster, job


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
    project = variant.project

    with transaction.atomic():
        PosterVariant.objects.filter(pk=variant.pk).update(
            is_deleted=True, is_selected=False
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
                    ProjectPosterStatus.READY if remaining else ProjectPosterStatus.EMPTY
                )
            poster.save(
                update_fields=["selected_variant", "status", "updated_at"]
            )

    return poster


# --------------------------------------------------------------------------- #
# Mock worker — replace with the real AI pipeline when it lands.
# --------------------------------------------------------------------------- #

# Placeholder image data: a 1x1 transparent PNG. The real worker will write
# the model's output bytes; until then we just persist a deterministic blob so
# the rest of the pipeline (selection, listing, FE rendering) can be exercised.
_PLACEHOLDER_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def complete_generation_mock(
    job: PosterGenerationJob,
    *,
    variant_count: int = 4,
) -> list[PosterVariant]:
    """Simulate a worker run: queued → processing → completed, with N variants.

    Idempotent for an already-completed job (returns its existing variants).
    Returning early when status is ``failed``/``cancelled`` keeps replay safe.
    """
    import base64

    from django.core.files.base import ContentFile

    if job.status == PosterJobStatus.COMPLETED:
        return list(job.variants.all())
    if job.status in (PosterJobStatus.FAILED, PosterJobStatus.CANCELLED):
        return []

    placeholder_bytes = base64.b64decode(_PLACEHOLDER_PNG_BASE64)

    with transaction.atomic():
        job.status = PosterJobStatus.PROCESSING
        job.started_at = timezone.now()
        job.save(update_fields=["status", "started_at", "updated_at"])

        created: list[PosterVariant] = []
        for idx in range(variant_count):
            variant = PosterVariant(
                job=job,
                poster=job.poster,
                project=job.project,
                user=job.user,
                variant_index=idx,
                width=job.width,
                height=job.height,
                mime_type="image/png",
            )
            variant.image.save(
                f"poster_job_{job.id}_v{idx}.png",
                ContentFile(placeholder_bytes),
                save=False,
            )
            variant.save()
            created.append(variant)

        job.status = PosterJobStatus.COMPLETED
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "completed_at", "updated_at"])

        # Spec: "после новой генерации автоматически выбрать первый новый
        # variant". Always promote the first new variant — predictable for the
        # FE; the user can pick another via PATCH /select.
        poster = job.poster
        poster.status = ProjectPosterStatus.READY
        if created:
            first = created[0]
            PosterVariant.objects.filter(
                poster=poster, is_selected=True
            ).update(is_selected=False)
            PosterVariant.objects.filter(pk=first.pk).update(is_selected=True)
            first.is_selected = True
            poster.selected_variant = first
        poster.save(update_fields=["selected_variant", "status", "updated_at"])

    return created


def fail_generation_mock(
    job: PosterGenerationJob,
    *,
    error_message: str,
    error_code: str = "",
) -> None:
    """Mark a job failed and bubble the failure up to the poster row when the
    poster has no good variant yet."""

    with transaction.atomic():
        job.status = PosterJobStatus.FAILED
        job.error_message = error_message
        job.error_code = error_code or ""
        job.completed_at = timezone.now()
        job.save(
            update_fields=[
                "status", "error_message", "error_code",
                "completed_at", "updated_at",
            ]
        )

        poster = job.poster
        if poster.selected_variant_id is None:
            poster.status = ProjectPosterStatus.FAILED
            poster.save(update_fields=["status", "updated_at"])
