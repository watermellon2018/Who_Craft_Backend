"""Durable worker execution for the Reference Library queue."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from w_craft_back.movie.reference_library.errors import (
    ReferenceError,
    map_provider_error,
)
from w_craft_back.movie.reference_library.lifecycle import (
    ReferenceLeaseLost,
    claim_reference_job,
    confirm_reference_cancellation,
    fail_reference_job,
    heartbeat_reference_job,
    mark_reference_job_stage,
    mark_reference_provider_started,
    reference_job_lease_seconds,
)
from w_craft_back.movie.reference_library.models import (
    ReferenceGenerationJob,
    ReferenceJobStage,
    ReferenceJobStatus,
    ReferenceOperation,
    ReferenceVariant,
)
from w_craft_back.movie.reference_library.providers import (
    DeterministicReferenceMockProvider,
    resolve_pinned_reference_provider,
)
from w_craft_back.movie.reference_library.services import persist_reference_image_pair
from w_craft_back.services.image_generation.errors import (
    ImageProviderError,
    map_to_provider_error,
)
from w_craft_back.storage_gateway import (
    StorageGatewayError,
    delete_storage_key,
    normalize_image_bytes,
)


class _ReferenceCancellationRequested(RuntimeError):
    """Signal cooperative cancellation without converting it into a failure."""


def reference_provider_timeout_seconds() -> int:
    """Keep provider calls bounded by the currently configured worker lease."""

    try:
        configured = max(
            1,
            int(getattr(settings, "REFERENCE_PROVIDER_TIMEOUT_SECONDS", 90)),
        )
    except (TypeError, ValueError):
        configured = 90
    return min(configured, max(1, reference_job_lease_seconds() - 5))


def _cleanup_outputs(outputs: list[tuple[Any, Any, list[str]]]) -> None:
    asset_ids = [asset.id for pair in outputs for asset in pair[:2]]
    keys = [key for _asset, _thumb, pair_keys in outputs for key in pair_keys]
    ReferenceVariant.objects.filter(asset_id__in=asset_ids).delete()
    from w_craft_back.movie.project.dashboard_models import ProjectAsset

    ProjectAsset.objects.filter(id__in=asset_ids).delete()
    for key in keys:
        delete_storage_key(key)


@transaction.atomic
def _finalize_outputs(
    claimed: ReferenceGenerationJob,
    outputs: list[tuple[Any, Any, list[str]]],
) -> ReferenceGenerationJob:
    job = ReferenceGenerationJob.objects.select_for_update().get(pk=claimed.pk)
    now = timezone.now()
    if (
        claimed.lease_token is None
        or job.lease_token != claimed.lease_token
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        raise ReferenceLeaseLost()
    if job.status == ReferenceJobStatus.CANCELLATION_REQUESTED:
        raise _ReferenceCancellationRequested()
    if job.status != ReferenceJobStatus.PROCESSING:
        raise ReferenceLeaseLost()
    ReferenceVariant.objects.bulk_create(
        [
            ReferenceVariant(
                job=job,
                asset=asset,
                thumbnail_asset=thumbnail,
                variant_index=index,
                seed=str(index),
                provider_metadata={
                    "provider": job.provider,
                    "modelName": job.model_name,
                },
            )
            for index, (asset, thumbnail, _keys) in enumerate(outputs)
        ]
    )
    job.status = ReferenceJobStatus.COMPLETED
    job.stage = ReferenceJobStage.FINALIZED
    job.progress = 100
    job.completed_at = now
    job.heartbeat_at = now
    job.lease_token = None
    job.lease_expires_at = None
    job.save()
    return job


def _provider_for_job(job: ReferenceGenerationJob):
    if job.provider == "mock":
        return DeterministicReferenceMockProvider()
    provider = resolve_pinned_reference_provider(
        actor=job.actor,
        requested_model=job.requested_model,
        require_edit=job.operation == ReferenceOperation.EDIT,
    )
    if provider.name != job.provider or provider.model_id != job.model_name:
        raise ReferenceError(
            "The pinned image provider configuration changed.",
            code="IMAGE_PROVIDER_NOT_CONFIGURED",
            http_status=503,
            retryable=True,
        )
    return provider


def execute_reference_job(job_id=None) -> ReferenceGenerationJob | None:
    """Claim and execute one reference job without local-path assumptions."""

    claimed = claim_reference_job(job_id)
    if claimed is None:
        return None
    if claimed.status == ReferenceJobStatus.CANCELLATION_REQUESTED:
        return confirm_reference_cancellation(claimed)
    outputs: list[tuple[Any, Any, list[str]]] = []
    try:
        current = mark_reference_job_stage(claimed, ReferenceJobStage.COMPILING, 10)
        if current.status == ReferenceJobStatus.CANCELLATION_REQUESTED:
            return confirm_reference_cancellation(claimed)
        provider = _provider_for_job(claimed)
        mark_reference_provider_started(claimed)
        if not heartbeat_reference_job(claimed.id, claimed.lease_token):
            raise ReferenceLeaseLost()
        compiled = claimed.compiled_request
        provider_timeout = reference_provider_timeout_seconds()
        if claimed.operation == ReferenceOperation.EDIT:
            source = claimed.source_version
            if source is None:
                raise ReferenceError(
                    "Source version is missing.",
                    code="REFERENCE_VERSION_NOT_FOUND",
                    http_status=404,
                )
            with default_storage.open(source.asset.file.name, "rb") as source_file:
                payloads = [
                    provider.edit(
                        source_file.read(),
                        compiled.get("editInstruction", ""),
                        mime_type=source.asset.metadata.get("mime_type", "image/png"),
                        timeout=provider_timeout,
                    )
                ]
        else:
            per_call_timeout = max(
                1,
                provider_timeout // max(1, claimed.variant_count),
            )
            payloads = provider.generate(
                compiled.get("compiledPrompt", ""),
                aspect_ratio=compiled.get("metadata", {}).get("aspectRatio", "1:1"),
                variant_count=claimed.variant_count,
                timeout=per_call_timeout,
            )
        if not heartbeat_reference_job(claimed.id, claimed.lease_token):
            raise ReferenceLeaseLost()
        if len(payloads) != claimed.variant_count:
            raise ReferenceError(
                "Image provider returned an unexpected number of variants.",
                code="IMAGE_PROVIDER_BAD_RESPONSE",
                http_status=502,
                retryable=True,
            )
        current = mark_reference_job_stage(claimed, ReferenceJobStage.VALIDATING, 65)
        if current.status == ReferenceJobStatus.CANCELLATION_REQUESTED:
            return confirm_reference_cancellation(claimed)
        normalized = [normalize_image_bytes(payload) for payload in payloads]
        if not heartbeat_reference_job(claimed.id, claimed.lease_token):
            raise ReferenceLeaseLost()
        current = mark_reference_job_stage(claimed, ReferenceJobStage.STORING, 80)
        if current.status == ReferenceJobStatus.CANCELLATION_REQUESTED:
            return confirm_reference_cancellation(claimed)
        for index, image in enumerate(normalized):
            if not heartbeat_reference_job(claimed.id, claimed.lease_token):
                raise ReferenceLeaseLost()
            outputs.append(
                persist_reference_image_pair(
                    project=claimed.project,
                    actor=claimed.actor,
                    image=image,
                    title=f"{claimed.reference.title} — variant {index + 1}",
                    origin=claimed.operation,
                    reference_job_id=claimed.id,
                )
            )
            if not heartbeat_reference_job(claimed.id, claimed.lease_token):
                raise ReferenceLeaseLost()
        return _finalize_outputs(claimed, outputs)
    except ReferenceLeaseLost:
        _cleanup_outputs(outputs)
        return None
    except _ReferenceCancellationRequested:
        _cleanup_outputs(outputs)
        return confirm_reference_cancellation(claimed)
    except ImageProviderError as error:
        mapped = map_provider_error(error)
        fail_reference_job(
            claimed,
            code=mapped.code,
            detail=mapped.detail,
            http_status=mapped.http_status,
            retryable=mapped.retryable,
        )
    except StorageGatewayError as error:
        fail_reference_job(
            claimed,
            code=error.code,
            detail=error.message,
            http_status=error.http_status,
            retryable=False,
        )
    except ReferenceError as error:
        fail_reference_job(
            claimed,
            code=error.code,
            detail=error.detail,
            http_status=error.http_status,
            retryable=error.retryable,
        )
    except Exception as error:
        provider_error = map_to_provider_error(error)
        mapped = map_provider_error(provider_error)
        fail_reference_job(
            claimed,
            code=mapped.code,
            detail=mapped.detail,
            http_status=mapped.http_status,
            retryable=mapped.retryable,
        )
    _cleanup_outputs(outputs)
    return ReferenceGenerationJob.objects.get(pk=claimed.pk)


def execute_next_reference_job() -> ReferenceGenerationJob | None:
    """Execute the oldest eligible reference job."""

    return execute_reference_job()
