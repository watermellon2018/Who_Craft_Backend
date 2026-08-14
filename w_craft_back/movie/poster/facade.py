"""Project-scoped poster service facade."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from w_craft_back.movie.poster.errors import (
    PosterError,
    PosterImageTooLarge,
    PosterJobNotFound,
    PosterProviderCircuitOpen,
    PosterProviderFailure,
    PosterVariantDeleted,
    PosterVariantNotFound,
    ProjectAccessDenied,
    ProjectNotFound,
)
from w_craft_back.movie.poster.generation_guard import (
    ensure_provider_circuit_closed,
    max_input_bytes,
    provider_circuit_key,
    provider_timeout_seconds,
    record_provider_failure,
    record_provider_success,
    request_fingerprint,
)
from w_craft_back.movie.poster.models import (
    PosterGenerationJob,
    PosterJobOperation,
    PosterJobStatus,
    PosterVariant,
    ProjectPoster,
    ProjectPosterStatus,
)
from w_craft_back.movie.poster.services import (
    complete_generation,
    complete_generation_mock,
    enqueue_generation_job,
    fail_generation,
    InvalidProviderImage,
    list_recent_variants,
    mark_generation_processing,
    resolve_reference_asset,
    select_variant as _select_variant,
    serialize_job,
    serialize_job_history,
    serialize_poster,
    serialize_variant,
    soft_delete_variant,
)
from w_craft_back.movie.project import policy
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.permissions import (
    user_can_edit_project,
    user_has_project_access,
)
from w_craft_back.storage_gateway import (
    MediaTooLarge,
    StorageGatewayError,
    normalize_image_bytes,
)
from w_craft_back.services.image_generation import (
    ImageProviderError,
    resolve_provider_for_user,
)

logger = logging.getLogger(__name__)


DEFAULT_VARIANT_LIMIT = 8
MAX_VARIANT_LIMIT = 50


def _load_project(project_id: int) -> Project:
    try:
        return Project.objects.select_related("owner").get(pk=project_id)
    except Project.DoesNotExist as exc:
        raise ProjectNotFound("project not found") from exc


def _project_for_access(user: User, project_id: int) -> Project:
    project = _load_project(project_id)
    if not user_has_project_access(user, project):
        raise ProjectAccessDenied("you do not have access to this project")
    return project


def _project_for_edit(user: User, project_id: int) -> Project:
    project = _load_project(project_id)
    if not user_can_edit_project(user, project):
        raise ProjectAccessDenied("you do not have access to this project")
    return project


def _project_for_generation(user: User, project_id: int) -> Project:
    project = _load_project(project_id)
    if not policy.can(user, project, policy.Action.RUN_GENERATION):
        raise ProjectAccessDenied("generation is not permitted for this project")
    return project


def _service_idempotency_key(value: str) -> str:
    return value or f"service:{uuid.uuid4()}"


def _read_limited_file(file_field) -> bytes:
    limit = max_input_bytes()
    try:
        file_field.open("rb")
        data = file_field.read(limit + 1)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise PosterVariantNotFound("poster source image is unavailable") from exc
    finally:
        try:
            file_field.close()
        except (AttributeError, OSError):
            pass
    if len(data) > limit:
        raise PosterImageTooLarge("Poster source image exceeds the byte limit")
    return data


def _serialize_operation(
    project: Project,
    poster: ProjectPoster,
    job: PosterGenerationJob,
    *,
    request=None,
    replayed: bool = False,
) -> dict[str, Any]:
    recent = list_recent_variants(project, limit=DEFAULT_VARIANT_LIMIT)
    variants = list(
        PosterVariant.objects.filter(job=job, is_deleted=False)
        .order_by("variant_index", "created_at")
    )
    return {
        "job_id": job.id,
        "jobId": job.id,
        "status": job.status,
        "idempotentReplay": replayed,
        "job": serialize_job(job, request),
        "poster": serialize_poster(
            poster,
            recent_variants=recent,
            request=request,
        ),
        "variants": [serialize_variant(variant, request) for variant in variants],
    }


def _raise_stored_failure(job: PosterGenerationJob) -> None:
    raise PosterProviderFailure(
        job.error_message or "Poster provider request failed",
        code=job.error_code or PosterProviderFailure.code,
        http_status=job.error_http_status or PosterProviderFailure.http_status,
    )


def _provider_failure(job, provider_key: str | None, exc: ImageProviderError):
    logger.warning(
        "poster_generation_failed",
        extra={
            "job_id": job.id,
            "provider": provider_key,
            "error_code": exc.code,
        },
    )
    if provider_key and exc.http_status >= 500:
        record_provider_failure(provider_key)
    fail_generation(
        job,
        error_message=exc.message,
        error_code=exc.code,
        error_http_status=exc.http_status,
    )
    raise PosterProviderFailure(
        exc.message,
        code=exc.code,
        http_status=exc.http_status,
    ) from exc


def _raise_persistence_failure(job, exc: Exception) -> None:
    logger.error(
        "poster_generation_persistence_failed",
        extra={
            "job_id": job.id,
            "error_code": "POSTER_RESULT_PERSISTENCE_FAILED",
            "exception_type": type(exc).__name__,
        },
    )
    try:
        fail_generation(
            job,
            error_message="Generated poster could not be stored",
            error_code="POSTER_RESULT_PERSISTENCE_FAILED",
            error_http_status=500,
        )
    except Exception:  # noqa: BLE001 — the lease remains a recovery fallback
        pass
    raise PosterProviderFailure(
        "Generated poster could not be stored",
        code="POSTER_RESULT_PERSISTENCE_FAILED",
        http_status=500,
    ) from exc


def _complete_provider_result(job, images: list[bytes], provider_key: str) -> None:
    try:
        complete_generation(job, images)
    except InvalidProviderImage:
        invalid_output = ImageProviderError(
            code="IMAGE_PROVIDER_BAD_RESPONSE",
            message="Poster provider returned an invalid image",
            http_status=502,
        )
        _provider_failure(job, provider_key, invalid_output)
    except Exception as exc:  # noqa: BLE001 — storage/DB boundary
        _raise_persistence_failure(job, exc)
    record_provider_success(provider_key)


def _complete_mock_result(job, *, variant_count: int = 4) -> None:
    try:
        complete_generation_mock(job, variant_count=variant_count)
    except Exception as exc:  # noqa: BLE001 — storage/DB boundary
        _raise_persistence_failure(job, exc)


def _resolve_and_claim(job, user, model_override, *, require_edit: bool):
    try:
        provider = resolve_provider_for_user(
            user,
            override=model_override,
            require_edit=require_edit,
        )
    except ImageProviderError as exc:
        fail_generation(
            job,
            error_message=exc.message,
            error_code=exc.code,
            error_http_status=exc.http_status,
        )
        raise PosterProviderFailure(
            exc.message,
            code=exc.code,
            http_status=exc.http_status,
        ) from exc

    provider_key = provider_circuit_key(provider)
    try:
        ensure_provider_circuit_closed(provider_key)
    except PosterProviderCircuitOpen as exc:
        fail_generation(
            job,
            error_message=exc.message,
            error_code=exc.code,
            error_http_status=exc.http_status,
        )
        raise

    claimed = mark_generation_processing(
        job,
        provider_name=str(getattr(provider, "name", "")),
        model_name=str(getattr(provider, "model_id", "")),
    )
    return provider, provider_key, claimed


def get_project_poster(
    user: User,
    project_id: int,
    *,
    request=None,
    limit: int = DEFAULT_VARIANT_LIMIT,
) -> dict[str, Any]:
    project = _project_for_access(user, project_id)
    poster = (
        ProjectPoster.objects.select_related("selected_variant")
        .filter(project=project)
        .first()
    )
    recent = list_recent_variants(project, limit=limit)
    poster_payload = (
        serialize_poster(
            poster,
            recent_variants=recent,
            request=request,
        )
        if poster is not None
        else {
            "id": None,
            "projectId": project.id,
            "status": ProjectPosterStatus.EMPTY,
            "selectedVariant": None,
            "recentVariants": [],
            "updatedAt": None,
        }
    )
    return {
        "poster": poster_payload,
        "recentVariants": [serialize_variant(v, request) for v in recent],
    }


def generate_poster(
    user: User,
    project_id: int,
    *,
    prompt: str,
    style: str,
    format: str,
    idempotency_key: str = "",
    reference_image_bytes: bytes | None = None,
    reference_mime_type: str = "image/png",
    reference_image_url: str = "",
    reference_image_asset_id: Optional[int] = None,
    image_model: str | None = None,
    request=None,
    run_mock: bool | None = None,
    execute_immediately: bool = True,
) -> dict[str, Any]:
    project = _project_for_generation(user, project_id)
    reference_asset = resolve_reference_asset(project, reference_image_asset_id)
    if reference_image_asset_id and reference_asset is None:
        raise PosterVariantNotFound("reference asset not found")
    if reference_image_url:
        raise PosterError(
            "Arbitrary reference URLs are not accepted; upload a project asset"
        )
    if reference_image_bytes is None and reference_asset is not None:
        reference_image_bytes = _read_limited_file(reference_asset.file)
        reference_mime_type = (
            (reference_asset.metadata or {}).get("mime_type") or "image/png"
        )
    if reference_image_bytes is not None:
        try:
            normalized_reference = normalize_image_bytes(
                reference_image_bytes,
                max_bytes=max_input_bytes(),
            )
        except MediaTooLarge as exc:
            raise PosterImageTooLarge(
                "Reference image exceeds the byte limit"
            ) from exc
        except StorageGatewayError as exc:
            raise PosterError(
                "Reference image is invalid",
                code="INVALID_REFERENCE_IMAGE",
            ) from exc
        reference_image_bytes = normalized_reference.data
        reference_mime_type = normalized_reference.mime_type

    reference_storage_key = ""
    if reference_image_bytes is not None:
        extension = {
            "image/jpeg": "jpg",
            "image/webp": "webp",
        }.get(reference_mime_type, "png")
        reference_storage_key = default_storage.save(
            f"projects/{project.id}/posters/references/{uuid.uuid4()}.{extension}",
            ContentFile(reference_image_bytes),
        )

    key = _service_idempotency_key(idempotency_key)
    fingerprint = request_fingerprint(
        {
            "operation": PosterJobOperation.GENERATE,
            "prompt": prompt,
            "style": style,
            "format": format,
            "reference_asset_id": reference_image_asset_id,
            "reference_mime_type": reference_mime_type,
            "image_model": image_model,
        },
        reference_image_bytes,
    )
    try:
        poster, job, created = enqueue_generation_job(
            project=project,
            user=user,
            prompt=prompt,
            style=style,
            format=format,
            operation=PosterJobOperation.GENERATE,
            idempotency_key=key,
            request_hash=fingerprint,
            requested_model=image_model or "",
            reference_storage_key=reference_storage_key,
            reference_mime_type=reference_mime_type if reference_storage_key else "",
            reference_asset=reference_asset,
        )
    except Exception:
        if reference_storage_key:
            default_storage.delete(reference_storage_key)
        raise
    if not created:
        if reference_storage_key:
            default_storage.delete(reference_storage_key)
        if job.status == PosterJobStatus.FAILED:
            _raise_stored_failure(job)
        return _serialize_operation(
            project, poster, job, request=request, replayed=True
        )

    logger.info(
        "poster_generation_queued",
        extra={
            "job_id": job.id,
            "project_id": project.id,
            "operation": PosterJobOperation.GENERATE,
        },
    )

    if not execute_immediately:
        return _serialize_operation(project, poster, job, request=request)

    use_mock = (
        getattr(settings, "POSTER_GENERATION_USE_MOCK", settings.DEBUG)
        if run_mock is None
        else run_mock
    )
    if use_mock:
        _complete_mock_result(job)
    else:
        provider, provider_key, claimed = _resolve_and_claim(
            job,
            user,
            image_model,
            require_edit=False,
        )
        try:
            if reference_image_bytes is not None:
                generate_with_reference = getattr(
                    provider,
                    "generate_with_reference",
                    None,
                )
                if generate_with_reference is None:
                    raise PosterError(
                        "Selected provider does not support reference images"
                    )
                images = generate_with_reference(
                    prompt,
                    reference_image_bytes,
                    mime_type=reference_mime_type,
                    variant_count=1,
                    timeout=provider_timeout_seconds(),
                )
            else:
                images = provider.generate(
                    prompt,
                    aspect_ratio=claimed.aspect_ratio,
                    variant_count=1,
                    timeout=provider_timeout_seconds(),
                )
            if not isinstance(images, list) or len(images) != 1:
                raise ImageProviderError(
                    code="IMAGE_PROVIDER_BAD_RESPONSE",
                    message="Provider must return exactly one poster image",
                    http_status=502,
                )
        except ImageProviderError as exc:
            _provider_failure(claimed, provider_key, exc)
        except PosterError as exc:
            fail_generation(
                claimed,
                error_message=exc.message,
                error_code=exc.code,
                error_http_status=exc.http_status,
            )
            raise
        except Exception:  # noqa: BLE001
            mapped = ImageProviderError(
                code="IMAGE_PROVIDER_UNAVAILABLE",
                message="Poster provider is unavailable",
                http_status=503,
            )
            _provider_failure(claimed, provider_key, mapped)
        else:
            _complete_provider_result(claimed, images, provider_key)

    job.refresh_from_db()
    poster.refresh_from_db()
    logger.info(
        "poster_generation_finished",
        extra={
            "job_id": job.id,
            "project_id": project.id,
            "operation": PosterJobOperation.GENERATE,
        },
    )
    return _serialize_operation(project, poster, job, request=request)


def edit_poster(
    user: User,
    project_id: int,
    *,
    source_variant_id: int,
    instruction: str,
    idempotency_key: str = "",
    image_model: str | None = None,
    request=None,
    run_mock: bool | None = None,
    execute_immediately: bool = True,
) -> dict[str, Any]:
    project = _project_for_generation(user, project_id)
    source = (
        PosterVariant.objects
        .select_related("job")
        .filter(pk=source_variant_id, project=project, is_deleted=False)
        .first()
    )
    if source is None:
        raise PosterVariantNotFound("source poster variant not found")
    source_bytes = _read_limited_file(source.image)
    source_mime_type = source.mime_type or "image/png"
    extension = {
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }.get(source_mime_type, "png")
    source_storage_key = default_storage.save(
        f"projects/{project.id}/posters/edit-sources/{uuid.uuid4()}.{extension}",
        ContentFile(source_bytes),
    )

    key = _service_idempotency_key(idempotency_key)
    fingerprint = request_fingerprint(
        {
            "operation": PosterJobOperation.EDIT,
            "source_variant_id": source.id,
            "instruction": instruction,
            "image_model": image_model,
        }
    )
    try:
        poster, job, created = enqueue_generation_job(
            project=project,
            user=user,
            prompt=instruction,
            style=source.job.style,
            format=source.job.format,
            operation=PosterJobOperation.EDIT,
            idempotency_key=key,
            request_hash=fingerprint,
            reference_storage_key=source_storage_key,
            reference_mime_type=source_mime_type,
            source_variant=source,
            requested_model=image_model or "",
        )
    except Exception:
        default_storage.delete(source_storage_key)
        raise
    if not created:
        default_storage.delete(source_storage_key)
        if job.status == PosterJobStatus.FAILED:
            _raise_stored_failure(job)
        return _serialize_operation(
            project, poster, job, request=request, replayed=True
        )

    logger.info(
        "poster_generation_queued",
        extra={
            "job_id": job.id,
            "project_id": project.id,
            "operation": PosterJobOperation.EDIT,
        },
    )

    if not execute_immediately:
        return _serialize_operation(project, poster, job, request=request)

    use_mock = (
        getattr(settings, "POSTER_GENERATION_USE_MOCK", settings.DEBUG)
        if run_mock is None
        else run_mock
    )
    if use_mock:
        _complete_mock_result(job, variant_count=1)
    else:
        provider, provider_key, claimed = _resolve_and_claim(
            job,
            user,
            image_model,
            require_edit=True,
        )
        try:
            edited = provider.edit(
                source_bytes,
                instruction,
                mime_type=source_mime_type,
                timeout=provider_timeout_seconds(),
            )
        except ImageProviderError as exc:
            _provider_failure(claimed, provider_key, exc)
        except Exception:  # noqa: BLE001
            mapped = ImageProviderError(
                code="IMAGE_PROVIDER_UNAVAILABLE",
                message="Poster provider is unavailable",
                http_status=503,
            )
            _provider_failure(claimed, provider_key, mapped)
        else:
            _complete_provider_result(claimed, [edited], provider_key)

    job.refresh_from_db()
    poster.refresh_from_db()
    logger.info(
        "poster_generation_finished",
        extra={
            "job_id": job.id,
            "project_id": project.id,
            "operation": PosterJobOperation.EDIT,
        },
    )
    return _serialize_operation(project, poster, job, request=request)




def get_poster_jobs(
    user: User,
    project_id: int,
    *,
    limit: int = 50,
    request=None,
) -> dict[str, Any]:
    project = _project_for_access(user, project_id)
    batch_limit = max(1, min(int(limit), 200))
    jobs = PosterGenerationJob.objects.filter(project=project).order_by("-created_at")
    return {
        "jobs": [serialize_job_history(job, request) for job in jobs[:batch_limit]],
    }


def retry_poster_generation(
    user: User,
    project_id: int,
    job_id: int,
    *,
    request=None,
) -> dict[str, Any]:
    from w_craft_back.movie.poster.lifecycle import retry_poster_job

    project = _project_for_generation(user, project_id)
    original = PosterGenerationJob.objects.filter(
        pk=job_id,
        project=project,
    ).first()
    if original is None:
        raise PosterJobNotFound("poster job not found")
    job = retry_poster_job(original, actor=user)
    return {
        "job_id": job.id,
        "jobId": job.id,
        "status": job.status,
        "job": serialize_job(job, request),
    }


def cancel_poster_generation(
    user: User,
    project_id: int,
    job_id: int,
    *,
    request=None,
) -> dict[str, Any]:
    from w_craft_back.movie.poster.lifecycle import request_poster_cancellation

    project = _project_for_generation(user, project_id)
    if not PosterGenerationJob.objects.filter(pk=job_id, project=project).exists():
        raise PosterJobNotFound("poster job not found")
    job = request_poster_cancellation(job_id)
    return {
        "job_id": job.id,
        "jobId": job.id,
        "status": job.status,
        "job": serialize_job(job, request),
    }

def get_poster_job(
    user: User,
    project_id: int,
    job_id: int,
    *,
    request=None,
) -> dict[str, Any]:
    project = _project_for_access(user, project_id)
    try:
        job = PosterGenerationJob.objects.select_related("poster").get(
            pk=job_id,
            project=project,
        )
    except PosterGenerationJob.DoesNotExist as exc:
        raise PosterJobNotFound("poster job not found") from exc

    variants = list(
        PosterVariant.objects.filter(job=job, is_deleted=False)
        .order_by("variant_index", "created_at")
    )
    return {
        "job": serialize_job(job, request),
        "variants": [serialize_variant(v, request) for v in variants],
    }


def select_poster_variant(
    user: User,
    project_id: int,
    variant_id: int,
    *,
    request=None,
) -> dict[str, Any]:
    project = _project_for_edit(user, project_id)
    variant = PosterVariant.objects.filter(pk=variant_id, project=project).first()
    if variant is None:
        raise PosterVariantNotFound("poster variant not found")
    if variant.is_deleted:
        raise PosterVariantDeleted("poster variant is deleted")

    poster = _select_variant(project=project, variant=variant)
    poster.refresh_from_db()
    variant.refresh_from_db()
    recent = list_recent_variants(project, limit=DEFAULT_VARIANT_LIMIT)
    return {
        "poster": {
            **serialize_poster(poster, recent_variants=recent, request=request),
            "selectedVariantId": poster.selected_variant_id,
        },
        "selectedVariant": serialize_variant(variant, request),
    }


def get_poster_variants(
    user: User,
    project_id: int,
    *,
    limit: int = DEFAULT_VARIANT_LIMIT,
    request=None,
) -> dict[str, Any]:
    project = _project_for_access(user, project_id)
    limit = max(1, min(MAX_VARIANT_LIMIT, int(limit or DEFAULT_VARIANT_LIMIT)))
    variants = list_recent_variants(project, limit=limit)
    return {"variants": [serialize_variant(v, request) for v in variants]}


def delete_poster_variant(
    user: User,
    project_id: int,
    variant_id: int,
    *,
    request=None,
) -> dict[str, Any]:
    project = _project_for_edit(user, project_id)
    variant = PosterVariant.objects.filter(pk=variant_id, project=project).first()
    if variant is None:
        raise PosterVariantNotFound("poster variant not found")
    if not variant.is_deleted:
        soft_delete_variant(variant=variant)
    return {"success": True}
