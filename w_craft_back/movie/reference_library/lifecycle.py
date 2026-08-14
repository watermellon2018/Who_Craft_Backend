"""Lease, fencing, cancellation, retry, and recovery for reference jobs."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from w_craft_back.movie.reference_library.errors import ReferenceConflict
from w_craft_back.movie.reference_library.models import (
    ProjectReference,
    ReferenceGenerationJob,
    ReferenceJobStage,
    ReferenceJobStatus,
)
from w_craft_back.credits.models import GenerationCharge
from w_craft_back.credits.services import (
    CreditServiceError,
    release_generation,
    reserve_generation,
)


TERMINAL_STATUSES = (
    ReferenceJobStatus.COMPLETED,
    ReferenceJobStatus.FAILED,
    ReferenceJobStatus.CANCELLED,
)


def _delete_unlinked_job_assets(job_id: uuid.UUID | str) -> None:
    """Delete crash-left temporary outputs that were never linked to a variant."""

    from w_craft_back.movie.project.dashboard_models import ProjectAsset

    ProjectAsset.objects.filter(
        metadata__reference_job_id=str(job_id),
        reference_variants__isnull=True,
        reference_thumbnail_variants__isnull=True,
        reference_versions__isnull=True,
        reference_thumbnail_versions__isnull=True,
    ).delete()


class ReferenceLeaseLost(RuntimeError):
    """Raised when a worker no longer owns a live execution fence."""


def reference_job_lease_seconds() -> int:
    try:
        return max(5, int(getattr(settings, "REFERENCE_JOB_LEASE_SECONDS", 120)))
    except (TypeError, ValueError):
        return 120


@transaction.atomic
def claim_reference_job(
    job_id: uuid.UUID | str | None = None,
    *,
    lease_seconds: int | None = None,
) -> ReferenceGenerationJob | None:
    """Claim the oldest queued/cancellation job and issue a new lease token."""

    queryset = ReferenceGenerationJob.objects.select_for_update(
        skip_locked=True,
    ).filter(
        status__in=(
            ReferenceJobStatus.QUEUED,
            ReferenceJobStatus.CANCELLATION_REQUESTED,
        ),
    )
    if job_id is not None:
        queryset = queryset.filter(pk=job_id)
    job = queryset.order_by("created_at").first()
    if job is None:
        return None
    now = timezone.now()
    if job.status == ReferenceJobStatus.QUEUED:
        if job.attempts >= job.max_attempts:
            job.status = ReferenceJobStatus.FAILED
            job.stage = ReferenceJobStage.FAILED
            job.error_code = "REFERENCE_MAX_ATTEMPTS_EXCEEDED"
            job.error_detail = "Reference generation retry limit reached."
            job.error_http_status = 409
            job.error_retryable = False
            job.completed_at = now
            job.save()
            release_generation(
                domain="reference",
                job_id=str(job.id),
                reason=job.error_code,
            )
            return None
        job.status = ReferenceJobStatus.PROCESSING
        job.stage = ReferenceJobStage.COMPILING
        job.progress = 5
        job.attempts += 1
        job.started_at = job.started_at or now
        job.provider_started_at = None
        job.error_code = ""
        job.error_detail = ""
        job.error_http_status = None
        job.error_retryable = None
    job.lease_token = uuid.uuid4()
    job.heartbeat_at = now
    ttl = lease_seconds if lease_seconds is not None else reference_job_lease_seconds()
    job.lease_expires_at = now + timedelta(seconds=max(1, int(ttl)))
    job.save()
    return job


def heartbeat_reference_job(
    job_id: uuid.UUID | str,
    lease_token: uuid.UUID,
    *,
    lease_seconds: int | None = None,
) -> bool:
    """Renew a currently owned lease."""

    now = timezone.now()
    ttl = lease_seconds if lease_seconds is not None else reference_job_lease_seconds()
    return ReferenceGenerationJob.objects.filter(
        pk=job_id,
        lease_token=lease_token,
        status__in=(
            ReferenceJobStatus.PROCESSING,
            ReferenceJobStatus.CANCELLATION_REQUESTED,
        ),
        lease_expires_at__gt=now,
    ).update(
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=max(1, int(ttl))),
        updated_at=now,
    ) == 1


def _locked_owned_job(claimed: ReferenceGenerationJob) -> ReferenceGenerationJob:
    locked = ReferenceGenerationJob.objects.select_for_update().get(pk=claimed.pk)
    now = timezone.now()
    if (
        claimed.lease_token is None
        or locked.lease_token != claimed.lease_token
        or locked.lease_expires_at is None
        or locked.lease_expires_at <= now
        or locked.status
        not in (
            ReferenceJobStatus.PROCESSING,
            ReferenceJobStatus.CANCELLATION_REQUESTED,
        )
    ):
        raise ReferenceLeaseLost()
    return locked


@transaction.atomic
def mark_reference_job_stage(
    claimed: ReferenceGenerationJob,
    stage: str,
    progress: int,
) -> ReferenceGenerationJob:
    """Advance progress only while the worker owns the live fence."""

    locked = _locked_owned_job(claimed)
    locked.stage = stage
    locked.progress = max(0, min(99, int(progress)))
    locked.save(update_fields=["stage", "progress", "updated_at"])
    claimed.stage = locked.stage
    claimed.progress = locked.progress
    return locked


@transaction.atomic
def mark_reference_provider_started(claimed: ReferenceGenerationJob) -> None:
    locked = _locked_owned_job(claimed)
    locked.provider_started_at = timezone.now()
    locked.stage = ReferenceJobStage.GENERATING
    locked.progress = 20
    locked.save(
        update_fields=["provider_started_at", "stage", "progress", "updated_at"]
    )
    claimed.provider_started_at = locked.provider_started_at


@transaction.atomic
def cancel_reference_job(job_id: uuid.UUID | str) -> ReferenceGenerationJob:
    """Cancel queued jobs immediately or request cooperative processing cancel."""

    job = ReferenceGenerationJob.objects.select_for_update().get(pk=job_id)
    if job.status in TERMINAL_STATUSES:
        raise ReferenceConflict(
            "Generation job is not cancellable.",
            code="REFERENCE_JOB_NOT_CANCELLABLE",
        )
    now = timezone.now()
    job.cancellation_requested_at = now
    if job.status == ReferenceJobStatus.QUEUED:
        job.status = ReferenceJobStatus.CANCELLED
        job.stage = ReferenceJobStage.CANCELLED
        job.progress = 0
        job.completed_at = now
        job.lease_token = None
        job.lease_expires_at = None
    else:
        job.status = ReferenceJobStatus.CANCELLATION_REQUESTED
    job.save()
    if job.status == ReferenceJobStatus.CANCELLED:
        release_generation(
            domain="reference",
            job_id=str(job.id),
            reason="cancelled",
        )
    return job


@transaction.atomic
def confirm_reference_cancellation(
    claimed: ReferenceGenerationJob,
) -> ReferenceGenerationJob:
    locked = _locked_owned_job(claimed)
    if locked.status != ReferenceJobStatus.CANCELLATION_REQUESTED:
        raise ReferenceConflict(
            "Generation job is not awaiting cancellation.",
            code="REFERENCE_JOB_NOT_CANCELLABLE",
        )
    now = timezone.now()
    locked.status = ReferenceJobStatus.CANCELLED
    locked.stage = ReferenceJobStage.CANCELLED
    locked.completed_at = now
    locked.heartbeat_at = now
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.save()
    release_generation(
        domain="reference",
        job_id=str(locked.id),
        reason="cancelled",
    )
    return locked


@transaction.atomic
def fail_reference_job(
    claimed: ReferenceGenerationJob,
    *,
    code: str,
    detail: str,
    http_status: int = 502,
    retryable: bool = False,
) -> bool:
    """Persist a safe failure only if the supplied fence is still current."""

    locked = ReferenceGenerationJob.objects.select_for_update().get(pk=claimed.pk)
    if locked.status in TERMINAL_STATUSES:
        return False
    if (
        claimed.lease_token is None
        or locked.lease_token != claimed.lease_token
        or locked.lease_expires_at is None
        or locked.lease_expires_at <= timezone.now()
    ):
        return False
    now = timezone.now()
    if locked.status == ReferenceJobStatus.CANCELLATION_REQUESTED:
        locked.status = ReferenceJobStatus.CANCELLED
        locked.stage = ReferenceJobStage.CANCELLED
        locked.completed_at = now
        locked.heartbeat_at = now
        locked.lease_token = None
        locked.lease_expires_at = None
        locked.save()
        release_generation(
            domain="reference",
            job_id=str(locked.id),
            reason="cancelled",
        )
        return False
    locked.status = ReferenceJobStatus.FAILED
    locked.stage = ReferenceJobStage.FAILED
    locked.error_code = str(code)[:128]
    locked.error_detail = str(detail)[:500]
    locked.error_http_status = max(400, min(599, int(http_status)))
    locked.error_retryable = bool(retryable)
    locked.completed_at = now
    locked.heartbeat_at = now
    locked.lease_token = None
    locked.lease_expires_at = None
    locked.save()
    release_generation(
        domain="reference",
        job_id=str(locked.id),
        reason=locked.error_code,
    )
    return True


@transaction.atomic
def recover_stale_reference_jobs(*, limit: int = 100) -> dict[str, list[uuid.UUID]]:
    """Requeue or terminate jobs whose execution lease expired."""

    now = timezone.now()
    jobs = list(
        ReferenceGenerationJob.objects.select_for_update(skip_locked=True)
        .filter(
            status__in=(
                ReferenceJobStatus.PROCESSING,
                ReferenceJobStatus.CANCELLATION_REQUESTED,
            ),
            lease_expires_at__lte=now,
        )
        .order_by("lease_expires_at")[:max(1, min(int(limit), 1000))]
    )
    recovered: list[uuid.UUID] = []
    failed: list[uuid.UUID] = []
    for job in jobs:
        _delete_unlinked_job_assets(job.id)
        if job.status == ReferenceJobStatus.CANCELLATION_REQUESTED:
            job.status = ReferenceJobStatus.CANCELLED
            job.stage = ReferenceJobStage.CANCELLED
            job.completed_at = now
            recovered.append(job.id)
        elif job.provider_started_at is not None:
            job.status = ReferenceJobStatus.FAILED
            job.stage = ReferenceJobStage.FAILED
            job.error_code = "IMAGE_PROVIDER_OUTCOME_UNKNOWN"
            job.error_detail = (
                "The provider request may have completed after the worker "
                "lease expired."
            )
            job.error_http_status = 503
            job.error_retryable = True
            job.completed_at = now
            failed.append(job.id)
        elif job.attempts >= job.max_attempts:
            job.status = ReferenceJobStatus.FAILED
            job.stage = ReferenceJobStage.FAILED
            job.error_code = "REFERENCE_MAX_ATTEMPTS_EXCEEDED"
            job.error_detail = "Reference generation retry limit reached."
            job.error_http_status = 409
            job.error_retryable = False
            job.completed_at = now
            failed.append(job.id)
        else:
            job.status = ReferenceJobStatus.QUEUED
            job.stage = ReferenceJobStage.QUEUED
            job.progress = 0
            job.provider_started_at = None
            recovered.append(job.id)
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
        if job.status in TERMINAL_STATUSES:
            release_generation(
                domain="reference",
                job_id=str(job.id),
                reason=job.error_code or job.status,
            )
    return {"recovered": recovered, "failed": failed}


def retry_reference_job(
    job_id: uuid.UUID | str,
    *,
    actor: Any,
) -> ReferenceGenerationJob:
    """Create one immutable-snapshot retry row for a failed/cancelled job."""

    with transaction.atomic():
        original = ReferenceGenerationJob.objects.select_for_update().get(pk=job_id)
        reference = ProjectReference.objects.select_for_update().get(
            pk=original.reference_id,
        )
        if original.status not in (
            ReferenceJobStatus.FAILED,
            ReferenceJobStatus.CANCELLED,
        ):
            raise ReferenceConflict(
                "Only failed or cancelled jobs can be retried.",
                code="REFERENCE_JOB_NOT_RETRYABLE",
            )
        if reference.archived_at is not None:
            raise ReferenceConflict(
                "Archived references cannot be retried.",
                code="REFERENCE_ARCHIVED",
            )
        existing = original.retries.order_by("created_at").first()
        if existing:
            return existing
        if reference.generation_jobs.filter(
            status__in=(
                ReferenceJobStatus.QUEUED,
                ReferenceJobStatus.PROCESSING,
                ReferenceJobStatus.CANCELLATION_REQUESTED,
            )
        ).exists():
            raise ReferenceConflict(
                "A generation job is already active for this reference.",
                code="REFERENCE_JOB_ALREADY_ACTIVE",
                retryable=True,
            )
        retry = ReferenceGenerationJob.objects.create(
            project=original.project,
            reference=reference,
            actor=actor,
            operation=original.operation,
            brief_snapshot=original.brief_snapshot,
            compiled_request=original.compiled_request,
            source_version=original.source_version,
            variant_count=original.variant_count,
            requested_model=original.requested_model,
            idempotency_key=f"retry:{original.id}",
            request_fingerprint=original.request_fingerprint,
            max_attempts=original.max_attempts,
            retry_of=original,
            provider=original.provider,
            model_name=original.model_name,
        )
        original_charge = GenerationCharge.objects.filter(
            domain="reference",
            job_id=str(original.id),
        ).first()
        if original_charge is not None:
            try:
                reserve_generation(
                    user=actor.user,
                    domain="reference",
                    job_id=str(retry.id),
                    provider=original_charge.provider,
                    model_name=original_charge.model_name,
                    estimated_cost=original_charge.estimated_cost,
                    reservation_amount=original_charge.reserved_amount,
                    pricing_snapshot=original_charge.pricing_snapshot,
                )
            except CreditServiceError as error:
                raise ReferenceConflict(
                    error.message,
                    code=error.code,
                ) from error
        return retry
