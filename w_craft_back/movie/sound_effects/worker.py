"""Worker dispatcher for durable sound-effect jobs."""

from __future__ import annotations

import logging

from django.conf import settings

from w_craft_back.movie.sound_effects.errors import (
    SoundEffectError,
    SoundEffectProviderError,
    public_provider_detail,
)
from w_craft_back.movie.sound_effects.lifecycle import (
    claim_sound_effect_job,
    fail_sound_effect_job,
    finalize_sound_effect_job,
    mark_sound_effect_provider_started,
    SoundEffectExecutionContext,
)
from w_craft_back.movie.sound_effects.models import (
    SoundEffectAsset,
    SoundEffectGenerationJob,
    SoundEffectJobStage,
)
from w_craft_back.movie.sound_effects.providers import get_sound_effect_provider
from w_craft_back.movie.sound_effects.generation_access import (
    validate_sound_effect_snapshot_access,
)
from w_craft_back.personal_api.access import credential_for_candidate
from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.storage_gateway import (
    InvalidAudio,
    MediaTooLarge,
    StorageGatewayError,
    UnsupportedMedia,
    delete_storage_key,
    store_audio_bytes,
)


logger = logging.getLogger(__name__)


def _generate_personal_sound_effect(claimed, snapshot, context):
    access = snapshot["generationAccess"]
    last_error = None
    for index, candidate in enumerate(access["candidates"]):
        try:
            credential = credential_for_candidate(access, candidate, user=claimed.actor)
        except PersonalAPIError as exc:
            last_error = exc
            continue
        provider = get_sound_effect_provider(claimed.provider, api_key=credential)
        pinned = dict(snapshot, generationCandidateIndex=index)
        context.checkpoint()
        SoundEffectGenerationJob.objects.filter(
            pk=claimed.pk, lease_token=claimed.lease_token,
        ).update(provider_snapshot=pinned)
        claimed.provider_snapshot = pinned
        credential_for_candidate(access, candidate, user=claimed.actor)
        mark_sound_effect_provider_started(claimed)
        try:
            return provider.generate(claimed.request, context)
        except SoundEffectProviderError as exc:
            if not exc.definitely_rejected or exc.outcome_unknown or exc.cost_incurred:
                raise
            last_error = exc
    if last_error is not None:
        raise last_error
    raise SoundEffectProviderError(
        "No personal sound-effect route is available.", retryable=False,
    )


def execute_sound_effect_job(
    job_id=None,
) -> SoundEffectGenerationJob | None:
    """Claim, execute, validate, store, and settle one effect job."""

    claimed = claim_sound_effect_job(job_id)
    if claimed is None:
        if job_id is None:
            return None
        return SoundEffectGenerationJob.objects.filter(pk=job_id).first()
    stored_key = ""
    asset_id = None
    provider_started = False
    try:
        snapshot = dict(claimed.provider_snapshot or {})
        if snapshot.get("backendProvider") != claimed.provider:
            raise SoundEffectProviderError(
                "Stored provider snapshot is invalid.",
                code="SOUND_EFFECT_PROVIDER_NOT_CONFIGURED",
                retryable=False,
            )
        access = validate_sound_effect_snapshot_access(snapshot, user=claimed.actor)
        context = SoundEffectExecutionContext(claimed.pk, claimed.lease_token)
        context.checkpoint()
        if access["source"] == "personal":
            generated = _generate_personal_sound_effect(claimed, snapshot, context)
        else:
            provider = get_sound_effect_provider(claimed.provider)
            mark_sound_effect_provider_started(claimed)
            provider_started = True
            generated = provider.generate(claimed.request, context)
        SoundEffectGenerationJob.objects.filter(pk=claimed.pk).update(
            stage=SoundEffectJobStage.STORING
        )
        stored = store_audio_bytes(
            generated.payload,
            namespace=(
                f"projects/{claimed.project_id}/sound-effects/jobs/"
                f"{str(claimed.pk).lower()}/variant-1"
            ),
            max_bytes=int(
                getattr(settings, "MUSIC_MAX_OUTPUT_BYTES", 50 * 1024 * 1024)
            ),
            allowed_mime_types=("audio/mpeg",),
            min_duration_seconds=0.5,
            max_duration_seconds=30,
        )
        stored_key = stored.storage_key
        asset = SoundEffectAsset.objects.create(
            project=claimed.project,
            file=stored.storage_key,
            mime_type=stored.mime_type,
            size_bytes=stored.size_bytes,
            checksum_sha256=stored.sha256,
            duration_seconds=stored.duration_seconds,
            provider=claimed.provider,
            model_name=claimed.model_name,
            provider_request_id=generated.provider_request_id,
            provenance=dict(generated.provenance),
            created_by=claimed.actor,
        )
        asset_id = asset.pk
        return finalize_sound_effect_job(
            claimed,
            asset=asset,
            provider_metadata={
                "providerRequestId": generated.provider_request_id,
            },
        )
    except PersonalAPIError as exc:
        fail_sound_effect_job(
            claimed, code=exc.code, detail=exc.message,
            http_status=exc.status_code, retryable=False, cost_incurred=False,
        )
    except (InvalidAudio, UnsupportedMedia, MediaTooLarge) as exc:
        code = (
            "SOUND_EFFECT_OUTPUT_TOO_LARGE"
            if isinstance(exc, MediaTooLarge)
            else "SOUND_EFFECT_OUTPUT_INVALID"
        )
        fail_sound_effect_job(
            claimed,
            code=code,
            detail=public_provider_detail(code),
            http_status=502,
            retryable=False,
            cost_incurred=True,
        )
    except SoundEffectProviderError as exc:
        fail_sound_effect_job(
            claimed,
            code=exc.code,
            detail=public_provider_detail(exc.code),
            http_status=exc.http_status,
            retryable=bool(exc.retryable and not exc.outcome_unknown),
            cost_incurred=bool(exc.cost_incurred or exc.outcome_unknown),
        )
    except SoundEffectError as exc:
        fail_sound_effect_job(
            claimed,
            code=exc.code,
            detail=exc.detail,
            http_status=exc.http_status,
            retryable=exc.retryable,
            cost_incurred=provider_started,
        )
    except (OSError, StorageGatewayError):
        logger.exception(
            "sound_effect_storage_failed",
            extra={"job_id": str(claimed.pk)},
        )
        fail_sound_effect_job(
            claimed,
            code="SOUND_EFFECT_OUTPUT_INVALID",
            detail=public_provider_detail("SOUND_EFFECT_OUTPUT_INVALID"),
            http_status=502,
            retryable=False,
            cost_incurred=True,
        )
    except Exception:
        source = (claimed.provider_snapshot.get("generationAccess") or {}).get("source")
        unknown = source == "personal" and bool(claimed.provider_started_at)
        error_code = (
            "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN" if unknown
            else "SOUND_EFFECT_PROVIDER_UNAVAILABLE"
        )
        if source == "personal":
            logger.error(
                "personal_sound_effect_worker_failed",
                extra={"job_id": str(claimed.pk)},
            )
        else:
            logger.exception(
                "sound_effect_worker_failed",
                extra={"job_id": str(claimed.pk)},
            )
        fail_sound_effect_job(
            claimed,
            code=error_code,
            detail=public_provider_detail(error_code),
            http_status=503,
            retryable=not unknown,
            cost_incurred=provider_started,
        )
    if stored_key:
        if asset_id is not None:
            SoundEffectAsset.objects.filter(pk=asset_id).delete()
        try:
            delete_storage_key(stored_key)
        except (OSError, StorageGatewayError):
            logger.exception("sound_effect_orphan_cleanup_failed")
    return SoundEffectGenerationJob.objects.get(pk=claimed.pk)


def execute_next_sound_effect_job() -> SoundEffectGenerationJob | None:
    return execute_sound_effect_job(None)
