"""Durable, fenced lifecycle operations for Music Studio generation jobs.

API code should call only :func:`enqueue_music_job`, :func:`retry_music_job`,
and :func:`request_music_cancellation`. Provider execution belongs to the worker;
all terminal writes verify the current lease token so a stale process cannot
publish duplicate variants.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from w_craft_back.movie.music.models import (
    MusicAsset,
    MusicAssetOrigin,
    MusicAssetRole,
    MusicAssetVerificationStatus,
    MusicGenerationJob,
    MusicJobStage,
    MusicJobStatus,
    MusicModerationStatus,
    MusicVariant,
    MusicVariantStatus,
)
from w_craft_back.movie.music.prompt_compiler import (
    MusicBriefError,
    compile_music_prompt,
    normalize_music_brief,
)
from w_craft_back.movie.music.providers import (
    MusicProviderError,
    get_music_provider,
    pricing_from_snapshot,
    resolve_audio_model,
    resolve_legacy_audio_route,
    resolved_from_snapshot,
)
from w_craft_back.movie.project.dashboard_models import MusicTrack, Scene
from w_craft_back.movie.project.models import Project
from w_craft_back.credits.services import (
    capture_generation,
    generation_charge_payload,
    release_generation,
    reserve_generation,
)


TERMINAL_MUSIC_JOB_STATUSES = frozenset(
    {MusicJobStatus.COMPLETED, MusicJobStatus.FAILED, MusicJobStatus.CANCELLED}
)


class MusicLifecycleError(RuntimeError):
    """Safe domain error that API adapters can map without exposing internals."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        http_status: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


class MusicLeaseLost(MusicLifecycleError):
    """Raised at a checkpoint after another worker acquired the fence."""

    def __init__(self) -> None:
        super().__init__(
            "Music generation lease was lost.",
            code="MUSIC_LEASE_LOST",
            http_status=409,
            retryable=False,
        )


class MusicGenerationCancelled(MusicLifecycleError):
    """Raised cooperatively when durable cancellation has been requested."""

    def __init__(self) -> None:
        super().__init__(
            "Music generation was cancelled.",
            code="MUSIC_CANCELLED",
            http_status=409,
            retryable=False,
        )


@dataclass(frozen=True)
class PersistedAudioCandidate:
    """Validated stored output waiting for one atomic ORM finalization."""

    storage_key: str
    mime_type: str
    size_bytes: int
    sha256: str
    duration_seconds: float
    seed: int | None
    provider_request_id: str
    provenance: Mapping[str, Any]
    result_snapshot: Mapping[str, Any]


@dataclass(frozen=True)
class ExecutionContext:
    """Provider-facing lease and cancellation checkpoint for one claimed job."""

    job_id: uuid.UUID
    lease_token: uuid.UUID

    def heartbeat(self) -> None:
        """Renew the owned lease or raise when the fence no longer matches."""

        if not heartbeat_music_job(self.job_id, self.lease_token):
            raise MusicLeaseLost()

    def is_cancelled(self) -> bool:
        """Read durable cancellation without retaining an ORM object."""

        return MusicGenerationJob.objects.filter(
            pk=self.job_id,
            status=MusicJobStatus.CANCELLATION_REQUESTED,
        ).exists()

    def checkpoint(self) -> None:
        """Stop on cancellation, otherwise heartbeat the current lease."""

        if self.is_cancelled():
            raise MusicGenerationCancelled()
        self.heartbeat()


def music_job_lease_seconds() -> int:
    """Return a bounded positive lease duration from settings."""

    try:
        value = int(getattr(settings, "MUSIC_JOB_LEASE_SECONDS", 60))
    except (TypeError, ValueError):
        value = 60
    return max(10, min(value, 3600))


def _scene_context(
    project: Project,
    normalized_brief: Mapping[str, Any],
) -> dict | None:
    context = normalized_brief.get("context") or {}
    if context.get("type") != "scene":
        return None
    scene = (
        Scene.objects.select_related("location")
        .filter(pk=context.get("sceneId"), project=project)
        .first()
    )
    if scene is None:
        raise MusicLifecycleError(
            "Scene was not found in this project.",
            code="MUSIC_SCENE_NOT_FOUND",
            http_status=404,
        )
    return {
        "sceneId": scene.pk,
        "title": scene.title,
        "durationSeconds": scene.duration_seconds,
        "mood": scene.mood,
        "sceneType": scene.scene_type,
        "summary": scene.description,
    }


def _fingerprint(
    *,
    project: Project,
    normalized_brief: Mapping[str, Any],
    variant_count: int,
    target_track: MusicTrack | None,
    reference_asset: MusicAsset | None,
    model_key: str,
) -> str:
    intent = {
        "projectId": project.pk,
        "brief": normalized_brief,
        "variantCount": variant_count,
        "targetTrackId": target_track.pk if target_track else None,
        "referenceAssetId": str(reference_asset.pk) if reference_asset else None,
        "modelKey": model_key,
    }
    payload = json.dumps(
        intent,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _legacy_provider_fingerprint(
    *,
    project: Project,
    normalized_brief: Mapping[str, Any],
    variant_count: int,
    target_track: MusicTrack | None,
    reference_asset: MusicAsset | None,
    provider_name: str,
    model_name: str,
) -> str:
    """Reproduce the pre-catalog fingerprint for queued-job compatibility."""

    intent = {
        "projectId": project.pk,
        "brief": normalized_brief,
        "variantCount": variant_count,
        "targetTrackId": target_track.pk if target_track else None,
        "referenceAssetId": str(reference_asset.pk) if reference_asset else None,
        "provider": provider_name,
        "model": model_name,
    }
    payload = json.dumps(
        intent,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_intent(
    *,
    project: Project,
    actor,
    normalized_brief: Mapping[str, Any],
    variant_count: int,
    target_track: MusicTrack | None,
    reference_asset: MusicAsset | None,
    capabilities,
) -> None:
    if actor is None or not getattr(actor, "pk", None):
        raise MusicLifecycleError(
            "A durable generation actor is required.",
            code="MUSIC_PERMISSION_DENIED",
            http_status=403,
        )
    if target_track and target_track.project_id != project.pk:
        raise MusicLifecycleError(
            "Target track was not found in this project.",
            code="MUSIC_TRACK_NOT_FOUND",
            http_status=404,
        )
    if reference_asset:
        if (
            reference_asset.project_id != project.pk
            or reference_asset.asset_role != MusicAssetRole.REFERENCE
        ):
            raise MusicLifecycleError(
                "Reference asset was not found in this project.",
                code="MUSIC_REFERENCE_NOT_FOUND",
                http_status=404,
            )
        if (
            reference_asset.verification_status
            != MusicAssetVerificationStatus.VERIFIED
            or not reference_asset.rights_confirmed_at
            or not reference_asset.rights_statement_version
        ):
            raise MusicLifecycleError(
                "Reference asset has not passed local rights/media validation.",
                code="MUSIC_REFERENCE_INVALID",
                http_status=400,
            )
    mode = normalized_brief["content"]["mode"]
    duration = normalized_brief["durationSeconds"]
    if (
        mode not in capabilities.content_modes
        or variant_count not in capabilities.variant_counts
    ):
        raise MusicLifecycleError(
            "The selected provider does not support this music request.",
            code="MUSIC_CAPABILITY_UNSUPPORTED",
            http_status=400,
        )
    if not (
        capabilities.min_duration_seconds
        <= duration
        <= capabilities.max_duration_seconds
    ):
        raise MusicLifecycleError(
            "The requested duration is unsupported.",
            code="MUSIC_CAPABILITY_UNSUPPORTED",
            http_status=400,
        )
    if reference_asset and not capabilities.supports_audio_reference:
        raise MusicLifecycleError(
            "The selected provider does not support audio references.",
            code="MUSIC_CAPABILITY_UNSUPPORTED",
            http_status=400,
        )
    if mode == "song":
        language = normalized_brief["content"].get("lyricsLanguage")
        if language not in capabilities.lyrics_languages:
            raise MusicLifecycleError(
                "The selected provider does not support this lyrics language.",
                code="MUSIC_LYRICS_UNSUPPORTED",
                http_status=400,
            )


def enqueue_music_job(
    *,
    project: Project,
    actor,
    brief: Mapping[str, Any],
    variant_count: int,
    idempotency_key: str,
    target_track: MusicTrack | None = None,
    reference_asset: MusicAsset | None = None,
    provider_name: str | None = None,
    model_name: str = "",
    model_key: str | None = None,
    provider_snapshot: Mapping[str, Any] | None = None,
) -> tuple[MusicGenerationJob, bool]:
    """Create a durable queued job or return its idempotent replay.

    This function performs no provider/network work. The returned boolean is
    ``True`` only when the same actor/key/fingerprint already existed.
    """

    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise MusicLifecycleError(
            "A valid Idempotency-Key is required.",
            code="MUSIC_IDEMPOTENCY_REQUIRED",
            http_status=400,
        )
    try:
        normalized = normalize_music_brief(brief)
    except MusicBriefError as exc:
        raise MusicLifecycleError(
            str(exc), code=exc.code, http_status=exc.http_status
        ) from exc
    persisted_snapshot = dict(provider_snapshot or {})
    if persisted_snapshot:
        intent_model_key = str(persisted_snapshot.get("modelKey") or "").strip()
    elif model_key:
        intent_model_key = str(model_key).strip().lower()
    elif provider_name:
        intent_model_key = (
            f"legacy:{str(provider_name).strip().lower()}:"
            f"{str(model_name).strip().lower()}"
        )
    else:
        # An omitted selector is part of the caller's intent. Keeping it empty
        # makes a replay independent from later default-model configuration.
        intent_model_key = ""
    fingerprint = _fingerprint(
        project=project,
        normalized_brief=normalized,
        variant_count=int(variant_count),
        target_track=target_track,
        reference_asset=reference_asset,
        model_key=intent_model_key,
    )
    with transaction.atomic():
        existing = (
            MusicGenerationJob.objects.select_for_update()
            .filter(project=project, actor=actor, idempotency_key=key)
            .first()
        )
        if existing is not None:
            matches = existing.request_fingerprint == fingerprint
            if not matches and not model_key and not persisted_snapshot:
                matches = existing.request_fingerprint == _legacy_provider_fingerprint(
                    project=project,
                    normalized_brief=normalized,
                    variant_count=int(variant_count),
                    target_track=target_track,
                    reference_asset=reference_asset,
                    provider_name=existing.provider,
                    model_name=existing.model_name,
                )
            if not matches:
                raise MusicLifecycleError(
                    "Idempotency key was already used for another request.",
                    code="MUSIC_IDEMPOTENCY_CONFLICT",
                    http_status=409,
                )
            return existing, True
    if persisted_snapshot:
        restored = resolved_from_snapshot(persisted_snapshot)
        if (
            restored.route.backend_name == "mock"
            and not restored.route.configured()
        ):
            raise MusicProviderError(
                "The mock audio model is disabled.",
                code="MUSIC_MODEL_NOT_CONFIGURED",
                http_status=503,
                retryable=False,
            )
        effective_provider = restored.route.backend_name
        effective_model = restored.route.model_id
        capabilities = restored.model.capabilities
        pricing = pricing_from_snapshot(persisted_snapshot)
        provider = get_music_provider(
            effective_provider,
            model_name=effective_model,
        )
    else:
        resolved = (
            resolve_audio_model(model_key)
            if model_key or not provider_name
            else resolve_legacy_audio_route(
                provider_name,
                model_name,
                require_configured=True,
            )
        )
        effective_provider = resolved.route.backend_name
        effective_model = resolved.route.model_id
        provider = get_music_provider(
            effective_provider,
            model_name=effective_model,
        )
        capabilities = resolved.model.capabilities
        pricing = resolved.pricing(int(variant_count))
        persisted_snapshot = resolved.snapshot(int(variant_count))
        # Preserve dependency-injected providers used by existing integrations/tests.
        if provider.name != effective_provider:
            pricing = provider.pricing(int(variant_count))
            persisted_snapshot["pricing"] = dict(pricing.snapshot)
            persisted_snapshot["estimatedCostUsd"] = str(pricing.estimated_cost)
    _validate_intent(
        project=project,
        actor=actor,
        normalized_brief=normalized,
        variant_count=int(variant_count),
        target_track=target_track,
        reference_asset=reference_asset,
        capabilities=capabilities,
    )
    scene_context = _scene_context(project, normalized)
    try:
        compiled = compile_music_prompt(
            normalized,
            scene_context=scene_context,
            reference_asset_id=reference_asset.pk if reference_asset else None,
            variant_count=int(variant_count),
        )
    except MusicBriefError as exc:
        raise MusicLifecycleError(
            str(exc), code=exc.code, http_status=exc.http_status
        ) from exc
    # Preserve an explicit advanced seed; otherwise derive an idempotent one.
    compiled.setdefault("baseSeed", int(fingerprint[:8], 16))

    with transaction.atomic():
        existing = (
            MusicGenerationJob.objects.select_for_update()
            .filter(project=project, actor=actor, idempotency_key=key)
            .first()
        )
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise MusicLifecycleError(
                    "Idempotency key was already used for another request.",
                    code="MUSIC_IDEMPOTENCY_CONFLICT",
                    http_status=409,
                )
            return existing, True
        try:
            with transaction.atomic():
                job = MusicGenerationJob.objects.create(
                    project=project,
                    actor=actor,
                    target_track=target_track,
                    reference_asset=reference_asset,
                    brief=normalized,
                    compiled_request=compiled,
                    provider=effective_provider,
                    model_name=effective_model,
                    provider_snapshot=persisted_snapshot,
                    variant_count=int(variant_count),
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                )
                reserve_generation(
                    user=actor,
                    domain="music",
                    job_id=str(job.id),
                    provider=effective_provider,
                    model_name=effective_model,
                    estimated_cost=pricing.estimated_cost,
                    reservation_amount=pricing.estimated_cost,
                    pricing_snapshot=dict(pricing.snapshot),
                    project=project,
                    operation="generate",
                )
        except IntegrityError:
            existing = MusicGenerationJob.objects.select_for_update().get(
                project=project,
                actor=actor,
                idempotency_key=key,
            )
            if existing.request_fingerprint != fingerprint:
                raise MusicLifecycleError(
                    "Idempotency key was already used for another request.",
                    code="MUSIC_IDEMPOTENCY_CONFLICT",
                    http_status=409,
                )
            return existing, True
    return job, False


def _available_lease(now) -> Q:
    return Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)


@transaction.atomic
def claim_music_job(
    job_id: uuid.UUID | str | None = None,
    *,
    lease_seconds: int | None = None,
) -> MusicGenerationJob | None:
    """Atomically claim one queued/due/cancellation job with a new fence."""

    now = timezone.now()
    available = _available_lease(now)
    due_poll = (
        Q(status=MusicJobStatus.PROCESSING)
        & ~Q(provider_job_id="")
        & (Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
        & available
    )
    eligible = (
        Q(status=MusicJobStatus.QUEUED)
        | (Q(status=MusicJobStatus.CANCELLATION_REQUESTED) & available)
        | due_poll
    )
    queryset = MusicGenerationJob.objects.select_for_update(skip_locked=True).filter(
        eligible
    )
    if job_id is not None:
        queryset = queryset.filter(pk=job_id)
    job = queryset.order_by("created_at").first()
    if job is None:
        return None

    if job.status == MusicJobStatus.QUEUED and job.attempts >= job.max_attempts:
        job.status = MusicJobStatus.FAILED
        job.stage = MusicJobStage.FAILED
        job.error_code = "MUSIC_MAX_ATTEMPTS_EXCEEDED"
        job.error_detail = "Music generation retry limit reached."
        job.error_http_status = 409
        job.error_retryable = False
        job.completed_at = now
        job.save()
        release_generation(
            domain="music",
            job_id=str(job.id),
            reason=job.error_code,
        )
        return None

    if job.status == MusicJobStatus.QUEUED:
        job.status = MusicJobStatus.PROCESSING
        job.stage = (
            MusicJobStage.PREPARING_REFERENCE
            if job.reference_asset_id
            else MusicJobStage.GENERATING
        )
        job.attempts += 1
        job.started_at = job.started_at or now
        job.provider_started_at = None
        job.error_code = ""
        job.error_detail = ""
        job.error_http_status = None
        job.error_retryable = None
    job.lease_token = uuid.uuid4()
    job.heartbeat_at = now
    ttl = lease_seconds if lease_seconds is not None else music_job_lease_seconds()
    job.lease_expires_at = now + timedelta(seconds=max(1, int(ttl)))
    job.save()
    return job


def heartbeat_music_job(
    job_id: uuid.UUID | str,
    lease_token: uuid.UUID,
    *,
    lease_seconds: int | None = None,
) -> bool:
    """Renew an owned lease and fail closed when status/token changed."""

    now = timezone.now()
    ttl = lease_seconds if lease_seconds is not None else music_job_lease_seconds()
    updated = MusicGenerationJob.objects.filter(
        pk=job_id,
        lease_token=lease_token,
        status__in=(
            MusicJobStatus.PROCESSING,
            MusicJobStatus.CANCELLATION_REQUESTED,
        ),
    ).update(
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=max(1, int(ttl))),
        updated_at=now,
    )
    return updated == 1


@transaction.atomic
def request_music_cancellation(
    job_or_id: MusicGenerationJob | uuid.UUID | str,
) -> MusicGenerationJob:
    """Cancel queued music work before provider execution starts."""

    job_id = job_or_id.pk if isinstance(job_or_id, MusicGenerationJob) else job_or_id
    job = MusicGenerationJob.objects.select_for_update().get(pk=job_id)
    if job.status in TERMINAL_MUSIC_JOB_STATUSES:
        return job
    if job.status == MusicJobStatus.CANCELLATION_REQUESTED:
        return job
    if job.status != MusicJobStatus.QUEUED:
        raise MusicLifecycleError(
            "Music generation can only be cancelled while it is queued.",
            code="MUSIC_CANNOT_CANCEL",
            http_status=409,
            retryable=False,
        )
    now = timezone.now()
    job.status = MusicJobStatus.CANCELLED
    job.stage = MusicJobStage.CANCELLED
    job.cancellation_requested_at = now
    job.completed_at = now
    job.lease_token = None
    job.lease_expires_at = None
    job.save()
    release_generation(
        domain="music",
        job_id=str(job.id),
        reason="cancelled_before_provider_start",
    )
    return job


def _settle_failed_music_job(job: MusicGenerationJob) -> None:
    """Settle paid work conservatively when provider cost may exist."""

    provider_result_received = bool(
        isinstance(job.provider_metadata, Mapping)
        and job.provider_metadata.get("resultReceived")
    )
    outcome_unknown = job.error_code == "MUSIC_PROVIDER_OUTCOME_UNKNOWN"
    if not (provider_result_received or outcome_unknown):
        release_generation(
            domain="music",
            job_id=str(job.id),
            reason=job.error_code,
        )
        return
    charge = generation_charge_payload("music", str(job.id)) or {}
    estimated_cost = Decimal(str(charge.get("estimatedCost") or "0"))
    capture_generation(
        domain="music",
        job_id=str(job.id),
        actual_cost=estimated_cost,
        provider_usage={
            "costSource": (
                "outcome-unknown-reservation"
                if outcome_unknown
                else "confirmed-provider-result"
            ),
            "costUsd": str(estimated_cost),
            "selectedProvider": job.provider,
            "selectedModel": job.model_name,
            "settlementReason": job.error_code,
        },
        cost_is_estimate=outcome_unknown,
    )


@transaction.atomic
def recover_stale_music_jobs(*, limit: int = 100) -> dict[str, list[uuid.UUID]]:
    """Recover expired leases without automatically duplicating unknown work."""

    now = timezone.now()
    batch_limit = max(1, min(int(limit), 1000))
    jobs = list(
        MusicGenerationJob.objects.select_for_update(skip_locked=True)
        .filter(
            status__in=(
                MusicJobStatus.PROCESSING,
                MusicJobStatus.CANCELLATION_REQUESTED,
            ),
            lease_expires_at__lte=now,
        )
        .order_by("lease_expires_at")[:batch_limit]
    )
    recovered: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    for job in jobs:
        blocking_outcome_unknown = (
            job.provider_started_at is not None and not job.provider_job_id
        )
        if blocking_outcome_unknown:
            job.status = MusicJobStatus.FAILED
            job.stage = MusicJobStage.FAILED
            job.error_code = "MUSIC_PROVIDER_OUTCOME_UNKNOWN"
            job.error_detail = "Provider outcome is unknown after lease expiry."
            job.error_http_status = 502
            job.error_retryable = False
            job.completed_at = now
            failed.append(job.pk)
        elif job.status == MusicJobStatus.CANCELLATION_REQUESTED:
            recovered.append(job.pk)
        elif job.provider_job_id:
            job.stage = MusicJobStage.POLLING
            recovered.append(job.pk)
        elif job.attempts >= job.max_attempts:
            job.status = MusicJobStatus.FAILED
            job.stage = MusicJobStage.FAILED
            job.error_code = "MUSIC_MAX_ATTEMPTS_EXCEEDED"
            job.error_detail = "Music generation retry limit reached."
            job.error_http_status = 409
            job.error_retryable = False
            job.completed_at = now
            failed.append(job.pk)
        else:
            job.status = MusicJobStatus.QUEUED
            job.stage = MusicJobStage.QUEUED
            job.provider_started_at = None
            recovered.append(job.pk)
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
        if job.status == MusicJobStatus.FAILED:
            _settle_failed_music_job(job)
    return {"recovered": recovered, "failed": failed}


def _locked_owned_job(claimed: MusicGenerationJob) -> MusicGenerationJob:
    locked = MusicGenerationJob.objects.select_for_update().get(pk=claimed.pk)
    if (
        claimed.lease_token is None
        or locked.lease_token != claimed.lease_token
        or locked.status
        not in (MusicJobStatus.PROCESSING, MusicJobStatus.CANCELLATION_REQUESTED)
    ):
        raise MusicLeaseLost()
    return locked


@transaction.atomic
def mark_music_job_stage(
    claimed: MusicGenerationJob,
    stage: str,
) -> MusicGenerationJob:
    """Advance a non-terminal stage only while the caller owns the fence."""

    locked = _locked_owned_job(claimed)
    locked.stage = stage
    locked.save(update_fields=("stage", "updated_at"))
    claimed.stage = stage
    return locked


@transaction.atomic
def record_music_reference_handle(
    claimed: MusicGenerationJob,
    reference_handle: str,
) -> MusicGenerationJob:
    """Persist an opaque provider reference handle under the active fence."""

    locked = _locked_owned_job(claimed)
    locked.provider_reference_id = str(reference_handle or "")[:255]
    locked.stage = MusicJobStage.GENERATING
    locked.save(
        update_fields=("provider_reference_id", "stage", "updated_at")
    )
    claimed.provider_reference_id = locked.provider_reference_id
    claimed.stage = locked.stage
    return locked


@transaction.atomic
def mark_music_provider_started(claimed: MusicGenerationJob) -> MusicGenerationJob:
    """Mark the cost/unknown-outcome boundary immediately before submit."""

    locked = _locked_owned_job(claimed)
    locked.provider_started_at = timezone.now()
    locked.stage = MusicJobStage.GENERATING
    locked.save(update_fields=("provider_started_at", "stage", "updated_at"))
    claimed.provider_started_at = locked.provider_started_at
    claimed.stage = locked.stage
    return locked


@transaction.atomic
def release_music_job_for_poll(
    claimed: MusicGenerationJob,
    *,
    provider_job_id: str,
    poll_after_seconds: float,
    provider_metadata: Mapping[str, Any] | None = None,
) -> MusicGenerationJob:
    """Persist an external handle and release the lease until its due poll."""

    handle = str(provider_job_id or "").strip()
    if not handle:
        raise ValueError("provider_job_id is required")
    locked = _locked_owned_job(claimed)
    locked.status = MusicJobStatus.PROCESSING
    locked.stage = MusicJobStage.POLLING
    locked.provider_job_id = handle[:255]
    locked.provider_metadata = dict(provider_metadata or {})
    locked.provider_started_at = None
    locked.next_poll_at = timezone.now() + timedelta(
        seconds=max(0.1, float(poll_after_seconds))
    )
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.save()
    return locked


@transaction.atomic
def mark_music_provider_result_received(
    claimed: MusicGenerationJob,
) -> MusicGenerationJob:
    """Persist the paid-result boundary before local validation and storage."""

    locked = _locked_owned_job(claimed)
    metadata = dict(locked.provider_metadata or {})
    metadata["resultReceived"] = True
    locked.provider_metadata = metadata
    locked.save(update_fields=("provider_metadata", "updated_at"))
    claimed.provider_metadata = metadata
    return locked


@transaction.atomic
def finalize_music_job(
    claimed: MusicGenerationJob,
    candidates: Sequence[PersistedAudioCandidate],
) -> MusicGenerationJob:
    """Atomically create assets/variants and complete a live fenced job."""

    locked = _locked_owned_job(claimed)
    if len(candidates) != locked.variant_count:
        raise MusicLifecycleError(
            "Provider returned an unexpected number of variants.",
            code="MUSIC_OUTPUT_INVALID",
            http_status=502,
            retryable=True,
        )
    if locked.variants.exists():
        raise MusicLifecycleError(
            "Music job has already published variants.",
            code="MUSIC_GENERATION_CONFLICT",
            http_status=409,
            retryable=False,
        )
    for index, candidate in enumerate(candidates):
        asset = MusicAsset(
            project=locked.project,
            file=candidate.storage_key,
            asset_role=MusicAssetRole.GENERATED,
            origin=MusicAssetOrigin.GENERATED,
            mime_type=candidate.mime_type,
            size_bytes=candidate.size_bytes,
            checksum_sha256=candidate.sha256,
            duration_seconds=Decimal(str(candidate.duration_seconds)).quantize(
                Decimal("0.001")
            ),
            verification_status=MusicAssetVerificationStatus.VERIFIED,
            moderation_status=MusicModerationStatus.NOT_REQUIRED,
            provider=locked.provider,
            model_name=locked.model_name,
            provider_request_id=candidate.provider_request_id,
            provenance=dict(candidate.provenance),
            created_by=locked.actor,
        )
        asset.save()
        MusicVariant.objects.create(
            job=locked,
            asset=asset,
            variant_index=index,
            seed=candidate.seed,
            status=MusicVariantStatus.GENERATED,
            provider_metadata=dict(candidate.result_snapshot),
        )
    now = timezone.now()
    locked.status = MusicJobStatus.COMPLETED
    locked.stage = MusicJobStage.FINALIZED
    locked.completed_at = now
    locked.heartbeat_at = now
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.next_poll_at = None
    locked.provider_started_at = None
    locked.error_code = ""
    locked.error_detail = ""
    locked.error_http_status = None
    locked.error_retryable = None
    locked.save()
    charge = generation_charge_payload("music", str(locked.id)) or {}
    actual_cost = Decimal(str(charge.get("estimatedCost") or "0"))
    capture_generation(
        domain="music",
        job_id=str(locked.id),
        actual_cost=actual_cost,
        provider_usage={
            "costSource": (
                "local" if locked.provider == "mock" else "fixed-provider-price"
            ),
            "costUsd": str(actual_cost),
            "selectedProvider": locked.provider,
            "selectedModel": locked.model_name,
        },
        cost_is_estimate=False,
    )
    return locked


@transaction.atomic
def fail_music_job(
    claimed: MusicGenerationJob,
    *,
    code: str,
    detail: str,
    http_status: int = 502,
    retryable: bool = False,
    cost_incurred: bool = False,
) -> bool:
    """Persist a safe failure only if the supplied lease fence is current."""

    locked = MusicGenerationJob.objects.select_for_update().get(pk=claimed.pk)
    if locked.status in TERMINAL_MUSIC_JOB_STATUSES:
        return False
    if claimed.lease_token is not None and locked.lease_token != claimed.lease_token:
        return False
    now = timezone.now()
    locked.status = MusicJobStatus.FAILED
    locked.stage = MusicJobStage.FAILED
    locked.error_code = str(code)[:128]
    locked.error_detail = str(detail)[:500]
    locked.error_http_status = max(400, min(int(http_status), 599))
    locked.error_retryable = bool(retryable)
    if cost_incurred:
        metadata = dict(locked.provider_metadata or {})
        metadata["resultReceived"] = True
        locked.provider_metadata = metadata
    locked.completed_at = now
    locked.heartbeat_at = now
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.next_poll_at = None
    locked.save()
    _settle_failed_music_job(locked)
    return True


@transaction.atomic
def confirm_music_cancellation(claimed: MusicGenerationJob) -> MusicGenerationJob:
    """Move cancellation_requested to terminal cancelled under the lease fence."""

    locked = _locked_owned_job(claimed)
    if locked.status != MusicJobStatus.CANCELLATION_REQUESTED:
        raise MusicLifecycleError(
            "Music job is not awaiting cancellation.",
            code="MUSIC_CANNOT_CANCEL",
            http_status=409,
        )
    now = timezone.now()
    locked.status = MusicJobStatus.CANCELLED
    locked.stage = MusicJobStage.CANCELLED
    locked.completed_at = now
    locked.heartbeat_at = now
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.next_poll_at = None
    locked.save()
    release_generation(
        domain="music",
        job_id=str(locked.id),
        reason="cancelled",
    )
    return locked


def retry_music_job(
    original: MusicGenerationJob,
    *,
    actor,
) -> MusicGenerationJob:
    """Create one new queued job from a terminal request snapshot.

    The original row remains terminal and immutable. Unknown blocking-provider
    outcomes cannot be retried automatically because doing so could duplicate a
    paid request.
    """

    with transaction.atomic():
        locked = MusicGenerationJob.objects.select_for_update().get(pk=original.pk)
        if locked.status not in (MusicJobStatus.FAILED, MusicJobStatus.CANCELLED):
            raise MusicLifecycleError(
                "Only failed or cancelled music jobs can be retried.",
                code="MUSIC_GENERATION_CONFLICT",
                http_status=409,
                retryable=True,
            )
        if locked.error_code == "MUSIC_PROVIDER_OUTCOME_UNKNOWN":
            raise MusicLifecycleError(
                "Provider outcome is unknown; automatic retry is unsafe.",
                code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                http_status=409,
            )
        existing = locked.retries.order_by("created_at").first()
        if existing is not None:
            return existing
        retry, _replay = enqueue_music_job(
            project=locked.project,
            actor=actor,
            brief=locked.brief,
            variant_count=locked.variant_count,
            idempotency_key=f"retry:{locked.pk}",
            target_track=locked.target_track,
            reference_asset=locked.reference_asset,
            provider_name=locked.provider,
            model_name=locked.model_name,
            model_key=str((locked.provider_snapshot or {}).get("modelKey") or ""),
            provider_snapshot=locked.provider_snapshot,
        )
        retry.retry_of = locked
        retry.max_attempts = locked.max_attempts
        retry.save(update_fields=("retry_of", "max_attempts", "updated_at"))
        return retry
