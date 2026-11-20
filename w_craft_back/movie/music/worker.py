"""Worker-side dispatcher for durable Music Studio generation jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from w_craft_back.movie.music.errors import public_provider_error_detail
from w_craft_back.movie.music.generation_access import validate_music_snapshot_access
from w_craft_back.personal_api.access import credential_for_candidate
from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.movie.music.lifecycle import (
    ExecutionContext,
    MusicGenerationCancelled,
    MusicLeaseLost,
    PersistedAudioCandidate,
    claim_music_job,
    confirm_music_cancellation,
    fail_music_job,
    finalize_music_job,
    mark_music_job_stage,
    mark_music_provider_started,
    mark_music_provider_result_received,
    record_music_reference_handle,
    release_music_job_for_poll,
)
from w_craft_back.movie.music.models import (
    MusicAsset,
    MusicGenerationJob,
    MusicJobStage,
    MusicJobStatus,
    MusicModerationStatus,
)
from w_craft_back.movie.music.providers import (
    MusicProviderError,
    ProviderSubmission,
    get_music_provider,
    resolved_from_snapshot,
)
from w_craft_back.storage_gateway import (
    InvalidAudio,
    MediaTooLarge,
    StorageGatewayError,
    UnsupportedMedia,
    delete_storage_key,
    store_generated_audio,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CancellationContext:
    """Allow upstream cancel checkpoints without treating the request as work."""

    execution: ExecutionContext

    def heartbeat(self) -> None:
        self.execution.heartbeat()

    def is_cancelled(self) -> bool:
        return True

    def checkpoint(self) -> None:
        self.execution.heartbeat()


def _mark_reference_moderation(job: MusicGenerationJob, status: str) -> None:
    if not job.reference_asset_id:
        return
    MusicAsset.objects.filter(pk=job.reference_asset_id).update(
        moderation_status=status,
        updated_at=timezone.now(),
    )


def _prepare_reference(
    claimed: MusicGenerationJob,
    provider,
    context: ExecutionContext,
) -> str:
    if not claimed.reference_asset_id:
        return ""
    mark_music_job_stage(claimed, MusicJobStage.PREPARING_REFERENCE)
    asset = claimed.reference_asset
    with asset.file.open("rb") as stream:
        handle = provider.prepare_reference(stream, context)
    context.checkpoint()
    record_music_reference_handle(claimed, handle)
    _mark_reference_moderation(claimed, MusicModerationStatus.ACCEPTED)
    return handle


def _cleanup_storage(keys: list[str]) -> None:
    for key in keys:
        try:
            delete_storage_key(key)
        except (OSError, StorageGatewayError):
            logger.exception("music_worker_orphan_cleanup_failed")


def _store_outputs(
    claimed: MusicGenerationJob,
    submission: ProviderSubmission,
    context: ExecutionContext,
) -> tuple[list[PersistedAudioCandidate], list[str]]:
    candidates: list[PersistedAudioCandidate] = []
    stored_keys: list[str] = []
    try:
        mark_music_job_stage(claimed, MusicJobStage.VALIDATING)
        for index, generated in enumerate(submission.outputs):
            context.checkpoint()
            stored = store_generated_audio(
                generated.payload,
                project_id=claimed.project_id,
                job_id=claimed.pk,
                variant_index=index,
            )
            stored_keys.append(stored.storage_key)
            if stored.mime_type != generated.mime_type:
                raise InvalidAudio("Provider MIME declaration does not match bytes.")
            if generated.duration_seconds is not None:
                duration_delta = abs(
                    float(stored.duration_seconds or 0)
                    - generated.duration_seconds
                )
                tolerance = max(0.25, generated.duration_seconds * 0.02)
                if duration_delta > tolerance:
                    raise InvalidAudio(
                        "Provider duration does not match audio bytes."
                    )
            candidates.append(
                PersistedAudioCandidate(
                    storage_key=stored.storage_key,
                    mime_type=stored.mime_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    duration_seconds=float(stored.duration_seconds or 0),
                    seed=generated.seed,
                    provider_request_id=generated.provider_request_id,
                    provenance=dict(generated.provenance),
                    result_snapshot=dict(generated.result_snapshot),
                )
            )
            context.checkpoint()
        mark_music_job_stage(claimed, MusicJobStage.STORING)
        return candidates, stored_keys
    except Exception:
        _cleanup_storage(stored_keys)
        raise


def _handle_submission(
    claimed: MusicGenerationJob,
    submission: ProviderSubmission,
    context: ExecutionContext,
) -> MusicGenerationJob:
    if submission.external_job_id:
        return release_music_job_for_poll(
            claimed,
            provider_job_id=submission.external_job_id,
            poll_after_seconds=submission.poll_after_seconds or 3.0,
            provider_metadata=submission.provider_metadata,
        )
    if submission.outputs:
        mark_music_provider_result_received(claimed)
    candidates, stored_keys = _store_outputs(claimed, submission, context)
    try:
        return finalize_music_job(claimed, candidates)
    except Exception:
        _cleanup_storage(stored_keys)
        raise


def _personal_provider(claimed, snapshot, index):
    """Pin the candidate before sending; resumed async jobs keep that candidate."""
    entry = snapshot["generationCandidates"][index]
    access = snapshot["generationAccess"]
    credential = credential_for_candidate(
        access, entry["candidate"], user=claimed.actor,
    )
    selected = dict(snapshot)
    selected.update(entry["snapshot"])
    selected["generationCandidateIndex"] = index
    updated = MusicGenerationJob.objects.filter(
        pk=claimed.pk, lease_token=claimed.lease_token,
    ).update(
        provider_snapshot=selected, provider=selected["backendProvider"],
        model_name=selected["modelName"],
    )
    if not updated:
        raise MusicLeaseLost()
    claimed.provider_snapshot = selected
    claimed.provider = selected["backendProvider"]
    claimed.model_name = selected["modelName"]
    return get_music_provider(
        claimed.provider, model_name=claimed.model_name, api_key=credential,
    ), entry["candidate"]


def _execute_personal_music(claimed, snapshot, context):
    access = snapshot["generationAccess"]
    if claimed.provider_job_id:
        provider, candidate = _personal_provider(
            claimed, snapshot, int(snapshot.get("generationCandidateIndex", 0)),
        )
        credential_for_candidate(access, candidate, user=claimed.actor)
        submission = provider.poll(
            claimed.provider_job_id, context, claimed.provider_metadata,
        )
        return _handle_submission(claimed, submission, context)
    last_error = None
    for index in range(len(snapshot["generationCandidates"])):
        try:
            provider, candidate = _personal_provider(claimed, snapshot, index)
        except PersonalAPIError as exc:
            last_error = exc
            continue
        try:
            credential_for_candidate(access, candidate, user=claimed.actor)
            reference_handle = _prepare_reference(claimed, provider, context)
            request = dict(claimed.compiled_request)
            if reference_handle:
                request["providerReferenceId"] = reference_handle
            context.checkpoint()
            credential_for_candidate(access, candidate, user=claimed.actor)
            mark_music_provider_started(claimed)
            submission = provider.submit(request, context)
        except MusicProviderError as exc:
            if not exc.definitely_rejected or exc.outcome_unknown or exc.cost_incurred:
                raise
            last_error = exc
            continue
        return _handle_submission(claimed, submission, context)
    if last_error is not None:
        raise last_error
    raise MusicProviderError(
        "No personal audio route is available.", code="MUSIC_PROVIDER_NOT_CONFIGURED",
        retryable=False,
    )


def execute_music_job(
    job_id=None,
) -> MusicGenerationJob | None:
    """Claim and execute one due music job, fencing every durable write."""

    claimed = claim_music_job(job_id)
    if claimed is None:
        if job_id is None:
            return None
        return MusicGenerationJob.objects.filter(pk=job_id).first()
    context = ExecutionContext(claimed.pk, claimed.lease_token)

    try:
        snapshot = dict(claimed.provider_snapshot or {})
        access = validate_music_snapshot_access(
            snapshot or {"backendProvider": claimed.provider}, user=claimed.actor,
        )
        if access["source"] == "personal":
            return _execute_personal_music(claimed, snapshot, context)
        restored = resolved_from_snapshot(snapshot) if snapshot else None
        provider = get_music_provider(
            (
                restored.route.backend_name
                if restored is not None
                else claimed.provider
            ),
            model_name=(
                restored.route.model_id
                if restored is not None
                else claimed.model_name
            ),
        )
        if claimed.status == MusicJobStatus.CANCELLATION_REQUESTED:
            if (
                claimed.provider_job_id
                and provider.capabilities().supports_cancellation
            ):
                provider.cancel(
                    claimed.provider_job_id,
                    _CancellationContext(context),
                )
            return confirm_music_cancellation(claimed)

        if claimed.provider_job_id:
            context.checkpoint()
            submission = provider.poll(
                claimed.provider_job_id,
                context,
                claimed.provider_metadata,
            )
            return _handle_submission(claimed, submission, context)

        reference_handle = _prepare_reference(claimed, provider, context)
        request = dict(claimed.compiled_request)
        if reference_handle:
            request["providerReferenceId"] = reference_handle
        context.checkpoint()
        mark_music_provider_started(claimed)
        submission = provider.submit(request, context)
        return _handle_submission(claimed, submission, context)
    except MusicGenerationCancelled:
        try:
            return confirm_music_cancellation(claimed)
        except MusicLeaseLost:
            pass
    except MusicLeaseLost:
        pass
    except PersonalAPIError as exc:
        fail_music_job(
            claimed, code=exc.code, detail=exc.message,
            http_status=exc.status_code, retryable=False,
        )
    except MediaTooLarge:
        fail_music_job(
            claimed,
            code="MUSIC_OUTPUT_TOO_LARGE",
            detail="Generated audio exceeds the configured byte limit.",
            http_status=502,
            retryable=False,
        )
    except (InvalidAudio, UnsupportedMedia):
        fail_music_job(
            claimed,
            code="MUSIC_OUTPUT_INVALID",
            detail="Provider returned invalid audio.",
            http_status=502,
            retryable=True,
        )
    except MusicProviderError as exc:
        if exc.code == "MUSIC_REFERENCE_REJECTED":
            _mark_reference_moderation(claimed, MusicModerationStatus.REJECTED)
        fail_music_job(
            claimed,
            code=exc.code,
            detail=(
                public_provider_error_detail(exc.code)
                or "Music provider is unavailable."
            ),
            http_status=exc.http_status,
            retryable=exc.retryable and not exc.outcome_unknown,
            cost_incurred=exc.cost_incurred,
        )
    except Exception:
        source = (claimed.provider_snapshot.get("generationAccess") or {}).get("source")
        unknown = source == "personal" and bool(claimed.provider_started_at)
        if source == "personal":
            logger.error(
                "personal_music_worker_failed", extra={"job_id": str(claimed.pk)},
            )
        else:
            logger.exception("music_worker_failed", extra={"job_id": str(claimed.pk)})
        fail_music_job(
            claimed,
            code=("MUSIC_PROVIDER_OUTCOME_UNKNOWN" if unknown else
                  "MUSIC_PROVIDER_UNAVAILABLE"),
            detail=("Music provider outcome is unknown." if unknown else
                    "Music provider is unavailable."),
            http_status=503,
            retryable=not unknown,
        )
    return MusicGenerationJob.objects.get(pk=claimed.pk)


def execute_next_music_job() -> MusicGenerationJob | None:
    """Claim and execute the oldest due music job, if one exists."""

    return execute_music_job(None)
