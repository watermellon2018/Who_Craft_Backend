"""Worker-side execution for durable poster jobs."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.files.storage import default_storage

from w_craft_back.movie.poster.errors import PosterError

from w_craft_back.movie.poster.generation_guard import (
    max_input_bytes,
    provider_circuit_key,
    provider_timeout_seconds,
    record_provider_failure,
    record_provider_success,
)
from w_craft_back.movie.poster.lifecycle import heartbeat_poster_job
from w_craft_back.movie.poster.models import (
    PosterGenerationJob,
    PosterJobOperation,
)
from w_craft_back.movie.poster.services import (
    complete_generation,
    InvalidProviderImage,
    complete_generation_mock,
    fail_generation,
    prepare_generation_images,
    start_generation_provider_call,
    mark_generation_processing,
)
from w_craft_back.services.image_generation import (
    ImageProviderError,
    resolve_provider_for_user,
)


logger = logging.getLogger(__name__)


def _read_file(file_field) -> bytes:
    file_field.open("rb")
    try:
        return file_field.read(max_input_bytes() + 1)
    finally:
        file_field.close()


def _reference_bytes(job: PosterGenerationJob) -> tuple[bytes | None, str]:
    if job.reference_storage_key:
        with default_storage.open(job.reference_storage_key, "rb") as handle:
            data = handle.read(max_input_bytes() + 1)
        return data, job.reference_mime_type or "image/png"
    if job.reference_asset_id:
        metadata = job.reference_asset.metadata or {}
        return _read_file(job.reference_asset.file), metadata.get("mime_type") or "image/png"
    return None, "image/png"

def execute_poster_job(job_id: int) -> PosterGenerationJob:
    """Claim and execute one poster job, fencing every terminal update."""
    job = (
        PosterGenerationJob.objects.select_related(
            "user",
            "reference_asset",
            "source_variant",
        ).get(pk=job_id)
    )
    use_mock = getattr(settings, "POSTER_GENERATION_USE_MOCK", settings.DEBUG)
    if use_mock:
        complete_generation_mock(
            job,
            variant_count=1 if job.operation == PosterJobOperation.EDIT else 4,
        )
        job.refresh_from_db()
        return job

    claimed = mark_generation_processing(
        job,
        provider_name="",
        model_name=job.requested_model,
    )
    if claimed is None:
        job.refresh_from_db()
        return job
    if not heartbeat_poster_job(claimed):
        job.refresh_from_db()
        return job

    try:
        provider = resolve_provider_for_user(
            claimed.user,
            override=claimed.requested_model or None,
            require_edit=claimed.operation == PosterJobOperation.EDIT,
        )

        try:
            if claimed.operation == PosterJobOperation.EDIT:
                source_bytes, source_mime_type = _reference_bytes(claimed)
                if source_bytes is None:
                    source = claimed.source_variant
                    if source is None:
                        raise ImageProviderError(
                            code="POSTER_SOURCE_MISSING",
                            message="Poster source image is unavailable",
                            http_status=400,
                        )
                    source_bytes = _read_file(source.image)
                    source_mime_type = source.mime_type or "image/png"
                reference_bytes = None
                reference_mime_type = "image/png"
            else:
                reference_bytes, reference_mime_type = _reference_bytes(claimed)
                source_bytes = None
                source_mime_type = "image/png"
        except ImageProviderError:
            raise
        except Exception:
            logger.exception(
                "poster_worker_input_read_failed",
                extra={"job_id": job_id},
            )
            fail_generation(
                claimed,
                error_message="Poster source image could not be read",
                error_code="POSTER_INPUT_UNAVAILABLE",
                error_http_status=500,
            )
            job.refresh_from_db()
            return job

        provider_key = provider_circuit_key(provider)
        started = start_generation_provider_call(
            claimed,
            provider_key=provider_key,
            provider_name=str(getattr(provider, "name", "")),
            model_name=str(getattr(provider, "model_id", "")),
        )
        if started is None:
            job.refresh_from_db()
            return job
        claimed = started

        if claimed.operation == PosterJobOperation.EDIT:
            image = provider.edit(
                source_bytes,
                claimed.prompt,
                mime_type=source_mime_type,
                timeout=provider_timeout_seconds(),
            )
            images = [image]
        elif reference_bytes is not None:
            generate_with_reference = getattr(provider, "generate_with_reference", None)
            if generate_with_reference is None:
                raise ImageProviderError(
                    code="IMAGE_PROVIDER_UNSUPPORTED_OPERATION",
                    message="Selected provider does not support reference images",
                    http_status=400,
                )
            images = generate_with_reference(
                claimed.prompt,
                reference_bytes,
                mime_type=reference_mime_type,
                variant_count=1,
                timeout=provider_timeout_seconds(),
            )
        else:
            images = provider.generate(
                claimed.prompt,
                aspect_ratio=claimed.aspect_ratio,
                variant_count=1,
                timeout=provider_timeout_seconds(),
            )

        if not isinstance(images, list) or not images:
            raise ImageProviderError(
                code="IMAGE_PROVIDER_BAD_RESPONSE",
                message="Poster provider returned no image",
                http_status=502,
            )
        try:
            prepared_images = prepare_generation_images(images)
        except InvalidProviderImage as exc:
            raise ImageProviderError(
                code="IMAGE_PROVIDER_BAD_RESPONSE",
                message="Poster provider returned an invalid image",
                http_status=502,
            ) from exc

        # A valid provider response closes a half-open probe even if local
        # persistence fails afterwards.
        record_provider_success(provider_key)
        if not heartbeat_poster_job(claimed):
            job.refresh_from_db()
            return job
        try:
            complete_generation(
                claimed,
                images,
                prepared_images=prepared_images,
                provider=provider,
            )
        except Exception:
            logger.exception(
                "poster_worker_persistence_failed",
                extra={"job_id": job_id},
            )
            fail_generation(
                claimed,
                error_message="Generated poster could not be stored",
                error_code="POSTER_RESULT_PERSISTENCE_FAILED",
                error_http_status=500,
            )
            job.refresh_from_db()
            return job
    except ImageProviderError as exc:
        if "provider_key" in locals():
            if exc.http_status >= 500:
                record_provider_failure(provider_key)
            else:
                record_provider_success(provider_key)
        fail_generation(
            claimed,
            error_message=exc.message,
            error_code=exc.code,
            error_http_status=exc.http_status,
        )
    except PosterError as exc:
        fail_generation(
            claimed,
            error_message=exc.message,
            error_code=exc.code,
            error_http_status=exc.http_status,
        )
    except Exception:  # Worker boundary persists an opaque public error.
        logger.exception("poster_worker_failed", extra={"job_id": job_id})
        if "provider_key" in locals():
            record_provider_failure(provider_key)
        fail_generation(
            claimed,
            error_message="Poster provider is unavailable",
            error_code="IMAGE_PROVIDER_UNAVAILABLE",
            error_http_status=503,
        )
    job.refresh_from_db()
    return job
