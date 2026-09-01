"""Lease and fencing lifecycle for durable Storyboard generations."""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from w_craft_back.credits.models import GenerationCharge
from w_craft_back.credits.services import capture_generation, release_generation
from w_craft_back.movie.storyboard.models import (
    StoryboardGenerationStatus,
    StoryboardKeyframeGeneration,
)


class StoryboardLeaseLost(RuntimeError):
    """Raised when a worker no longer owns a generation fence."""


def settle_failed_storyboard_generation(
    job: StoryboardKeyframeGeneration,
    *,
    reason: str,
    outcome_unknown: bool = False,
) -> None:
    """Release safe failures and conservatively capture ambiguous attempts."""

    if job.provider_result_received_at is None and not outcome_unknown:
        release_generation(
            domain="storyboard",
            job_id=str(job.pk),
            reason=reason,
        )
        return
    charge = GenerationCharge.objects.filter(
        domain="storyboard",
        job_id=str(job.pk),
    ).only("reserved_amount").first()
    if charge is None:
        return
    capture_generation(
        domain="storyboard",
        job_id=str(job.pk),
        actual_cost=charge.reserved_amount,
        provider_usage={
            "costSource": "reserved-estimate",
            "outcome": "unknown",
            "reason": str(reason)[:200],
        },
        cost_is_estimate=True,
    )


def storyboard_job_lease_seconds() -> int:
    try:
        configured = int(getattr(settings, "STORYBOARD_JOB_LEASE_SECONDS", 180))
    except (TypeError, ValueError):
        configured = 180
    try:
        provider_timeout = int(
            getattr(settings, "STORYBOARD_PROVIDER_TIMEOUT_SECONDS", 120)
        )
    except (TypeError, ValueError):
        provider_timeout = 120
    return max(configured, provider_timeout + 30, 60)


@transaction.atomic
def claim_storyboard_generation(job_id=None):
    now = timezone.now()
    queryset = StoryboardKeyframeGeneration.objects.select_for_update(
        skip_locked=True
    ).filter(
        status=StoryboardGenerationStatus.QUEUED,
        attempts__lt=models.F("max_attempts"),
    )
    if job_id is not None:
        queryset = queryset.filter(pk=job_id)
    job = queryset.order_by("created_at").first()
    if job is None:
        return None
    job.status = StoryboardGenerationStatus.GENERATING
    job.attempts += 1
    job.lease_token = uuid.uuid4()
    job.lease_expires_at = now + timedelta(seconds=storyboard_job_lease_seconds())
    job.heartbeat_at = now
    job.started_at = job.started_at or now
    job.save()
    return job


def _owned_locked(claimed: StoryboardKeyframeGeneration):
    try:
        job = StoryboardKeyframeGeneration.objects.select_for_update().get(
            pk=claimed.pk
        )
    except StoryboardKeyframeGeneration.DoesNotExist as error:
        raise StoryboardLeaseLost() from error
    if (
        claimed.lease_token is None
        or job.lease_token != claimed.lease_token
        or job.status != StoryboardGenerationStatus.GENERATING
        or job.lease_expires_at is None
        or job.lease_expires_at <= timezone.now()
    ):
        raise StoryboardLeaseLost()
    return job


@transaction.atomic
def heartbeat_storyboard_generation(job_id, lease_token) -> bool:
    now = timezone.now()
    expires = now + timedelta(seconds=storyboard_job_lease_seconds())
    updated = StoryboardKeyframeGeneration.objects.filter(
        pk=job_id,
        status=StoryboardGenerationStatus.GENERATING,
        lease_token=lease_token,
        lease_expires_at__gt=now,
    ).update(heartbeat_at=now, lease_expires_at=expires, updated_at=now)
    return bool(updated)


@transaction.atomic
def mark_storyboard_provider_started(claimed: StoryboardKeyframeGeneration):
    job = _owned_locked(claimed)
    now = timezone.now()
    job.provider_started_at = now
    job.heartbeat_at = now
    job.save(update_fields=["provider_started_at", "heartbeat_at", "updated_at"])
    claimed.provider_started_at = now
    return job


@transaction.atomic
def mark_storyboard_provider_result_received(
    claimed: StoryboardKeyframeGeneration,
):
    """Persist the cost boundary before validating or storing provider bytes."""

    job = _owned_locked(claimed)
    now = timezone.now()
    job.provider_result_received_at = now
    job.heartbeat_at = now
    job.save(
        update_fields=[
            "provider_result_received_at",
            "heartbeat_at",
            "updated_at",
        ]
    )
    claimed.provider_result_received_at = now
    return job


@transaction.atomic
def fail_storyboard_generation(
    claimed: StoryboardKeyframeGeneration,
    *,
    code: str,
    detail: str,
    outcome_unknown: bool = False,
) -> bool:
    try:
        job = _owned_locked(claimed)
    except StoryboardLeaseLost:
        return False
    now = timezone.now()
    job.status = StoryboardGenerationStatus.FAILED
    job.error_code = str(code)[:128]
    job.error_detail = str(detail)[:500]
    job.completed_at = now
    job.heartbeat_at = now
    job.lease_token = None
    job.lease_expires_at = None
    job.save()
    settle_failed_storyboard_generation(
        job,
        reason=job.error_code,
        outcome_unknown=outcome_unknown,
    )
    return True


@transaction.atomic
def recover_stale_storyboard_generations(*, limit: int = 100):
    now = timezone.now()
    jobs = list(
        StoryboardKeyframeGeneration.objects.select_for_update(skip_locked=True)
        .filter(
            status=StoryboardGenerationStatus.GENERATING,
            lease_expires_at__lte=now,
        )
        .order_by("lease_expires_at")[:max(1, min(int(limit), 1000))]
    )
    requeued: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    for job in jobs:
        if job.provider_started_at is not None:
            job.status = StoryboardGenerationStatus.FAILED
            job.error_code = "IMAGE_PROVIDER_OUTCOME_UNKNOWN"
            job.error_detail = (
                "The provider outcome is unknown after the worker lease expired."
            )
            job.completed_at = now
            failed.append(job.pk)
        elif job.attempts >= job.max_attempts:
            job.status = StoryboardGenerationStatus.FAILED
            job.error_code = "STORYBOARD_MAX_ATTEMPTS_EXCEEDED"
            job.error_detail = "Storyboard generation retry limit reached."
            job.completed_at = now
            failed.append(job.pk)
        else:
            job.status = StoryboardGenerationStatus.QUEUED
            job.provider_started_at = None
            requeued.append(job.pk)
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
        if job.status == StoryboardGenerationStatus.FAILED:
            settle_failed_storyboard_generation(
                job,
                reason=job.error_code,
                outcome_unknown=(job.provider_started_at is not None),
            )
    return {"requeued": requeued, "failed": failed}
