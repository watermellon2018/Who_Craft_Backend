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
from w_craft_back.movie.music.providers import get_music_provider
from w_craft_back.movie.project.dashboard_models import MusicTrack, Scene
from w_craft_back.movie.project.models import Project
from w_craft_back.credits.services import (
    capture_generation,
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


def _scene_context(project: Project, normalized_brief: Mapping[str, Any]) -> dict | None:
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
    provider_name: str,
    model_name: str,
) -> str:
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
    provider,
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
    capabilities = provider.capabilities()
    mode = normalized_brief["content"]["mode"]
    duration = normalized_brief["durationSeconds"]
    if mode not in capabilities.content_modes or variant_count not in capabilities.variant_counts:
        raise MusicLifecycleError(
            "The selected provider does not support this music request.",
            code="MUSIC_CAPABILITY_UNSUPPORTED",
            http_status=400,
        )
    if not capabilities.min_duration_seconds <= duration <= capabilities.max_duration_seconds:
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
    provider = get_music_provider(provider_name)
    effective_model = str(model_name or provider.model_name)
    _validate_intent(
        project=project,
        actor=actor,
        normalized_brief=normalized,
        variant_count=int(variant_count),
        target_track=target_track,
        reference_asset=reference_asset,
        provider=provider,
    )
    fingerprint = _fingerprint(
        project=project,
        normalized_brief=normalized,
        variant_count=int(variant_count),
        target_track=target_track,
        reference_asset=reference_asset,
        provider_name=provider.name,
        model_name=effective_model,
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
                    provider=provider.name,
                    model_name=effective_model,
                    variant_count=int(variant_count),
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                )
                if provider.name != "mock":
                    raise MusicLifecycleError(
                        "The selected music provider does not expose billing data.",
                        code="GENERATION_PRICE_UNAVAILABLE",
                        http_status=503,
                    )
                reserve_generation(
                    user=actor,
                    domain="music",
                    job_id=str(job.id),
                    provider=provider.name,
                    model_name=effective_model,
                    estimated_cost=Decimal("0"),
                    reservation_amount=Decimal("0"),
                    pricing_snapshot={
                        "currency": "USD",
                        "source": "local",
                        "markup": "0",
                        "creditUsdRate": "1",
                    },
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
    """Persist non-terminal cancellation; an owned worker confirms the stop."""

    job_id = job_or_id.pk if isinstance(job_or_id, MusicGenerationJob) else job_or_id
    job = MusicGenerationJob.objects.select_for_update().get(pk=job_id)
    if job.status in TERMINAL_MUSIC_JOB_STATUSES:
        return job
    if job.status != MusicJobStatus.CANCELLATION_REQUESTED:
        previous_status = job.status
        job.status = MusicJobStatus.CANCELLATION_REQUESTED
        job.cancellation_requested_at = timezone.now()
        if previous_status == MusicJobStatus.QUEUED:
            job.lease_token = None
            job.lease_expires_at = None
        job.save()
        release_generation(
            domain="music",
            job_id=str(job.id),
            reason="cancelled",
        )
    return job


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
            release_generation(
                domain="music",
                job_id=str(job.id),
                reason=job.error_code,
            )
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
    capture_generation(
        domain="music",
        job_id=str(locked.id),
        actual_cost=Decimal("0"),
        provider_usage={"costSource": "local", "costUsd": "0"},
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
    locked.completed_at = now
    locked.heartbeat_at = now
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.next_poll_at = None
    locked.save()
    release_generation(
        domain="music",
        job_id=str(locked.id),
        reason=locked.error_code,
    )
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
        )
        retry.retry_of = locked
        retry.max_attempts = locked.max_attempts
        retry.save(update_fields=("retry_of", "max_attempts", "updated_at"))
        return retry
