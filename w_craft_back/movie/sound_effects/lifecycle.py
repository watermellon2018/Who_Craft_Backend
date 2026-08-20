"""Transactional durable lifecycle for sound-effect generation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from w_craft_back.credits.services import (
    capture_generation,
    release_generation,
    reserve_generation,
)
from w_craft_back.movie.project.dashboard_models import Scene
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.sound_effects.errors import (
    SoundEffectError,
    public_provider_detail,
)
from w_craft_back.movie.sound_effects.models import (
    SoundEffect,
    SoundEffectGenerationJob,
    SoundEffectJobStage,
    SoundEffectJobStatus,
)
from w_craft_back.movie.sound_effects.providers import get_sound_effect_provider


DOMAIN = "sound_effect"
SNAPSHOT_VERSION = "sound-effect-provider-v1"


class SoundEffectExecutionContext:
    """Lease fence exposed to blocking provider reads."""

    def __init__(self, job_id: uuid.UUID, lease_token: uuid.UUID) -> None:
        self.job_id = job_id
        self.lease_token = lease_token

    def checkpoint(self) -> None:
        now = timezone.now()
        updated = SoundEffectGenerationJob.objects.filter(
            pk=self.job_id,
            lease_token=self.lease_token,
            status=SoundEffectJobStatus.PROCESSING,
        ).update(
            heartbeat_at=now,
            lease_expires_at=now
            + timedelta(seconds=sound_effect_lease_seconds()),
        )
        if not updated:
            raise SoundEffectError(
                "Sound-effect lease was lost.",
                code="SOUND_EFFECT_LEASE_LOST",
                http_status=409,
            )


def _fingerprint(
    *,
    project_id: int,
    request: Mapping[str, Any],
    target_effect_id: int | None,
    target_scene_id: int | None,
) -> str:
    payload = json.dumps(
        {
            "projectId": project_id,
            "request": request,
            "targetEffectId": target_effect_id,
            "sceneId": target_scene_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_snapshot(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("version") != SNAPSHOT_VERSION:
        raise SoundEffectError(
            "Stored provider snapshot is unsupported.",
            code="SOUND_EFFECT_PROVIDER_NOT_CONFIGURED",
            http_status=503,
        )
    if snapshot.get("backendProvider") != "elevenlabs-sfx" or (
        snapshot.get("modelName") != "eleven_text_to_sound_v2"
    ):
        raise SoundEffectError(
            "Stored provider route is unsupported.",
            code="SOUND_EFFECT_PROVIDER_NOT_CONFIGURED",
            http_status=503,
        )


def _snapshot_price(snapshot: Mapping[str, Any]) -> tuple[Decimal, dict[str, Any]]:
    _validate_snapshot(snapshot)
    try:
        estimated = Decimal(str(snapshot.get("estimatedCostUsd")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SoundEffectError(
            "Stored sound-effect price is invalid.",
            code="SOUND_EFFECT_PRICE_UNAVAILABLE",
            http_status=503,
        ) from exc
    if not estimated.is_finite() or estimated <= 0:
        raise SoundEffectError(
            "Stored sound-effect price is invalid.",
            code="SOUND_EFFECT_PRICE_UNAVAILABLE",
            http_status=503,
        )
    return estimated, dict(snapshot.get("pricing") or {})


def enqueue_sound_effect_job(
    *,
    project: Project,
    actor,
    request: Mapping[str, Any],
    idempotency_key: str,
    target_effect: SoundEffect | None = None,
    target_scene: Scene | None = None,
    provider_snapshot: Mapping[str, Any] | None = None,
) -> tuple[SoundEffectGenerationJob, bool]:
    """Reserve and enqueue exactly one immutable provider route."""

    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise SoundEffectError(
            "A valid Idempotency-Key is required.",
            code="SOUND_EFFECT_IDEMPOTENCY_REQUIRED",
            http_status=400,
        )
    if target_effect and target_effect.project_id != project.pk:
        raise SoundEffectError(
            "Target effect was not found in this project.",
            code="SOUND_EFFECT_NOT_FOUND",
            http_status=404,
        )
    if target_scene and target_scene.project_id != project.pk:
        raise SoundEffectError(
            "Target scene was not found in this project.",
            code="SOUND_EFFECT_SCENE_NOT_FOUND",
            http_status=404,
        )
    normalized_request = {
        "modelKey": str(request["modelKey"]),
        "prompt": str(request["prompt"]),
        "durationSeconds": request.get("durationSeconds"),
        "loop": bool(request.get("loop", False)),
        "promptInfluence": float(request.get("promptInfluence", 0.3)),
    }
    fingerprint = _fingerprint(
        project_id=project.pk,
        request=normalized_request,
        target_effect_id=target_effect.pk if target_effect else None,
        target_scene_id=target_scene.pk if target_scene else None,
    )

    # An idempotent replay is a logical request lookup. It must remain available
    # when provider configuration changes after the original enqueue.
    with transaction.atomic():
        existing = (
            SoundEffectGenerationJob.objects.select_for_update()
            .filter(project=project, actor=actor, idempotency_key=key)
            .first()
        )
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise SoundEffectError(
                    "Idempotency key was already used for another request.",
                    code="SOUND_EFFECT_IDEMPOTENCY_CONFLICT",
                    http_status=409,
                )
            return existing, True

    if provider_snapshot:
        snapshot = dict(provider_snapshot)
        estimated_cost, pricing_snapshot = _snapshot_price(snapshot)
    else:
        provider = get_sound_effect_provider()
        snapshot = provider.provider_snapshot(normalized_request["durationSeconds"])
        estimated_cost, pricing_snapshot = _snapshot_price(snapshot)

    with transaction.atomic():
        existing = (
            SoundEffectGenerationJob.objects.select_for_update()
            .filter(project=project, actor=actor, idempotency_key=key)
            .first()
        )
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise SoundEffectError(
                    "Idempotency key was already used for another request.",
                    code="SOUND_EFFECT_IDEMPOTENCY_CONFLICT",
                    http_status=409,
                )
            return existing, True
        try:
            with transaction.atomic():
                job = SoundEffectGenerationJob.objects.create(
                    project=project,
                    actor=actor,
                    target_effect=target_effect,
                    target_scene=target_scene,
                    request=normalized_request,
                    provider=str(snapshot["backendProvider"]),
                    model_name=str(snapshot["modelName"]),
                    provider_snapshot=snapshot,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                )
                reserve_generation(
                    user=actor,
                    domain=DOMAIN,
                    job_id=str(job.pk),
                    provider=job.provider,
                    model_name=job.model_name,
                    estimated_cost=estimated_cost,
                    reservation_amount=estimated_cost,
                    pricing_snapshot=pricing_snapshot,
                    project=project,
                    operation="generate",
                )
        except IntegrityError:
            existing = SoundEffectGenerationJob.objects.select_for_update().get(
                project=project,
                actor=actor,
                idempotency_key=key,
            )
            if existing.request_fingerprint != fingerprint:
                raise SoundEffectError(
                    "Idempotency key was already used for another request.",
                    code="SOUND_EFFECT_IDEMPOTENCY_CONFLICT",
                    http_status=409,
                )
            return existing, True
    return job, False


def sound_effect_lease_seconds() -> int:
    try:
        value = int(getattr(settings, "SOUND_EFFECTS_JOB_LEASE_SECONDS", 300))
    except (TypeError, ValueError):
        value = 300
    return max(30, min(value, 3600))


@transaction.atomic
def claim_sound_effect_job(
    job_id: uuid.UUID | str | None = None,
) -> SoundEffectGenerationJob | None:
    now = timezone.now()
    available = Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
    queryset = SoundEffectGenerationJob.objects.select_for_update(
        skip_locked=True
    ).filter(status=SoundEffectJobStatus.QUEUED).filter(available)
    if job_id is not None:
        queryset = queryset.filter(pk=job_id)
    job = queryset.order_by("created_at").first()
    if job is None:
        return None
    if job.attempts >= job.max_attempts:
        job.status = SoundEffectJobStatus.FAILED
        job.stage = SoundEffectJobStage.FAILED
        job.error_code = "SOUND_EFFECT_MAX_ATTEMPTS_EXCEEDED"
        job.completed_at = now
        job.save()
        release_generation(domain=DOMAIN, job_id=str(job.pk), reason="max-attempts")
        return None
    job.status = SoundEffectJobStatus.PROCESSING
    job.stage = SoundEffectJobStage.GENERATING
    job.attempts += 1
    job.lease_token = uuid.uuid4()
    job.lease_expires_at = now + timedelta(seconds=sound_effect_lease_seconds())
    job.heartbeat_at = now
    job.started_at = job.started_at or now
    job.save()
    return job


@transaction.atomic
def mark_sound_effect_provider_started(job: SoundEffectGenerationJob) -> None:
    updated = SoundEffectGenerationJob.objects.filter(
        pk=job.pk,
        lease_token=job.lease_token,
        status=SoundEffectJobStatus.PROCESSING,
    ).update(provider_started_at=timezone.now(), heartbeat_at=timezone.now())
    if not updated:
        raise SoundEffectError(
            "Sound-effect lease was lost.",
            code="SOUND_EFFECT_LEASE_LOST",
            http_status=409,
        )


@transaction.atomic
def finalize_sound_effect_job(
    job: SoundEffectGenerationJob,
    *,
    asset,
    provider_metadata: Mapping[str, Any],
) -> SoundEffectGenerationJob:
    from w_craft_back.movie.sound_effects.models import SoundEffectVariant

    locked = SoundEffectGenerationJob.objects.select_for_update().get(pk=job.pk)
    if locked.lease_token != job.lease_token:
        raise SoundEffectError(
            "Sound-effect lease was lost.",
            code="SOUND_EFFECT_LEASE_LOST",
            http_status=409,
        )
    SoundEffectVariant.objects.create(
        job=locked,
        asset=asset,
        provider_metadata=dict(provider_metadata),
    )
    now = timezone.now()
    locked.status = SoundEffectJobStatus.COMPLETED
    locked.stage = SoundEffectJobStage.FINALIZED
    locked.completed_at = now
    locked.heartbeat_at = now
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.error_code = ""
    locked.error_detail = ""
    locked.error_retryable = None
    locked.save()
    estimated, _pricing = _snapshot_price(locked.provider_snapshot)
    capture_generation(
        domain=DOMAIN,
        job_id=str(locked.pk),
        actual_cost=estimated,
        provider_usage={
            "selectedProvider": locked.provider,
            "selectedModel": locked.model_name,
            "providerRequestId": str(
                provider_metadata.get("providerRequestId") or ""
            ),
        },
        cost_is_estimate=True,
    )
    return locked


@transaction.atomic
def fail_sound_effect_job(
    job: SoundEffectGenerationJob,
    *,
    code: str,
    detail: str,
    http_status: int,
    retryable: bool,
    cost_incurred: bool,
) -> SoundEffectGenerationJob:
    locked = SoundEffectGenerationJob.objects.select_for_update().get(pk=job.pk)
    if locked.lease_token != job.lease_token:
        return locked
    now = timezone.now()
    locked.status = SoundEffectJobStatus.FAILED
    locked.stage = SoundEffectJobStage.FAILED
    locked.error_code = code
    locked.error_detail = detail[:500]
    locked.error_http_status = http_status
    locked.error_retryable = retryable
    locked.completed_at = now
    locked.heartbeat_at = now
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.save()
    if cost_incurred:
        estimated, _pricing = _snapshot_price(locked.provider_snapshot)
        capture_generation(
            domain=DOMAIN,
            job_id=str(locked.pk),
            actual_cost=estimated,
            provider_usage={
                "selectedProvider": locked.provider,
                "selectedModel": locked.model_name,
                "costSource": "ambiguous-paid-post",
            },
            cost_is_estimate=True,
        )
    else:
        release_generation(domain=DOMAIN, job_id=str(locked.pk), reason=code)
    return locked


@transaction.atomic
def cancel_sound_effect_job(job: SoundEffectGenerationJob) -> SoundEffectGenerationJob:
    locked = SoundEffectGenerationJob.objects.select_for_update().get(pk=job.pk)
    if locked.status != SoundEffectJobStatus.QUEUED:
        raise SoundEffectError(
            "Only queued sound effects can be cancelled.",
            code="SOUND_EFFECT_CANNOT_CANCEL",
            http_status=409,
        )
    locked.status = SoundEffectJobStatus.CANCELLED
    locked.stage = SoundEffectJobStage.CANCELLED
    locked.completed_at = timezone.now()
    locked.save()
    release_generation(domain=DOMAIN, job_id=str(locked.pk), reason="cancelled")
    return locked


@transaction.atomic
def retry_sound_effect_job(
    original: SoundEffectGenerationJob,
    *,
    actor,
) -> SoundEffectGenerationJob:
    locked = SoundEffectGenerationJob.objects.select_for_update().get(pk=original.pk)
    if locked.status not in (
        SoundEffectJobStatus.FAILED,
        SoundEffectJobStatus.CANCELLED,
    ):
        raise SoundEffectError(
            "Only failed or cancelled jobs can be retried.",
            code="SOUND_EFFECT_GENERATION_CONFLICT",
            http_status=409,
        )
    if locked.error_code == "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN":
        raise SoundEffectError(
            "Unknown paid provider outcomes cannot be retried automatically.",
            code="SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
            http_status=409,
        )
    existing = locked.retries.order_by("created_at").first()
    if existing is not None:
        return existing
    retry, _ = enqueue_sound_effect_job(
        project=locked.project,
        actor=actor,
        request=locked.request,
        idempotency_key=f"retry:{locked.pk}",
        target_effect=locked.target_effect,
        target_scene=locked.target_scene,
        provider_snapshot=locked.provider_snapshot,
    )
    retry.retry_of = locked
    retry.max_attempts = locked.max_attempts
    retry.save(update_fields=("retry_of", "max_attempts", "updated_at"))
    return retry


def recover_stale_sound_effect_jobs(
    *,
    limit: int = 100,
) -> dict[str, list[uuid.UUID]]:
    """Requeue unstarted leases and terminally capture ambiguous paid POSTs."""

    now = timezone.now()
    recovered: dict[str, list[uuid.UUID]] = {"requeued": [], "failed": []}
    job_ids = list(
        SoundEffectGenerationJob.objects.filter(
            status=SoundEffectJobStatus.PROCESSING,
            lease_expires_at__lte=now,
        )
        .order_by("lease_expires_at")
        .values_list("pk", flat=True)[: max(1, min(int(limit), 1000))]
    )
    for job_id in job_ids:
        with transaction.atomic():
            job = SoundEffectGenerationJob.objects.select_for_update().get(pk=job_id)
            if (
                job.status != SoundEffectJobStatus.PROCESSING
                or not job.lease_expires_at
                or job.lease_expires_at > now
            ):
                continue
            if job.provider_started_at:
                job.status = SoundEffectJobStatus.FAILED
                job.stage = SoundEffectJobStage.FAILED
                job.error_code = "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN"
                job.error_detail = public_provider_detail(
                    "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN"
                )
                job.error_http_status = 502
                job.error_retryable = False
                job.completed_at = now
                job.lease_token = None
                job.lease_expires_at = None
                job.save()
                estimated, _pricing = _snapshot_price(job.provider_snapshot)
                capture_generation(
                    domain=DOMAIN,
                    job_id=str(job.pk),
                    actual_cost=estimated,
                    provider_usage={
                        "selectedProvider": job.provider,
                        "selectedModel": job.model_name,
                        "costSource": "stale-ambiguous-paid-post",
                    },
                    cost_is_estimate=True,
                )
                recovered["failed"].append(job.pk)
            elif job.attempts < job.max_attempts:
                job.status = SoundEffectJobStatus.QUEUED
                job.stage = SoundEffectJobStage.QUEUED
                job.lease_token = None
                job.lease_expires_at = None
                job.save()
                recovered["requeued"].append(job.pk)
            else:
                job.status = SoundEffectJobStatus.FAILED
                job.stage = SoundEffectJobStage.FAILED
                job.error_code = "SOUND_EFFECT_MAX_ATTEMPTS_EXCEEDED"
                job.error_detail = "Sound-effect retry limit reached."
                job.error_http_status = 409
                job.error_retryable = False
                job.completed_at = now
                job.lease_token = None
                job.lease_expires_at = None
                job.save()
                release_generation(
                    domain=DOMAIN,
                    job_id=str(job.pk),
                    reason="max-attempts",
                )
                recovered["failed"].append(job.pk)
    return recovered
