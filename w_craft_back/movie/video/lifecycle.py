"""Short transactions fence every worker write; external calls never hold locks."""
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .errors import VideoLeaseLost
from .models import VideoGenerationJob


def lease_seconds() -> int:
    configured = int(getattr(settings, "VIDEO_JOB_LEASE_SECONDS", 300))
    download = int(getattr(settings, "VIDEO_DOWNLOAD_TIMEOUT_SECONDS", 180))
    return max(300, min(configured, 900), min(download, 600) + 90)


def _leased(job_id, token):
    return VideoGenerationJob.objects.filter(pk=job_id, lease_token=token,
                                             lease_expires_at__gt=timezone.now(),
                                             status="processing")


def checkpoint(job_id, token) -> None:
    expires = timezone.now() + timedelta(seconds=lease_seconds())
    if not _leased(job_id, token).update(lease_expires_at=expires):
        raise VideoLeaseLost()


def update_owned(job_id, token, **fields) -> None:
    if not _leased(job_id, token).update(updated_at=timezone.now(), **fields):
        raise VideoLeaseLost()


def claim_next_video_job():
    now = timezone.now()
    with transaction.atomic():
        job = VideoGenerationJob.objects.select_for_update(skip_locked=True).filter(
            Q(status="queued") | Q(status="processing", lease_token__isnull=True),
        ).filter(
            Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now),
        ).order_by("created_at").first()
        if job is None:
            return None
        job.status = "processing"
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = now + timedelta(seconds=lease_seconds())
        job.attempts += 1
        job.save(update_fields=[
            "status", "lease_token", "lease_expires_at", "attempts", "updated_at",
        ])
        return job


def defer_job(job, *, stage="polling", failed=False) -> None:
    update_owned(
        job.pk, job.lease_token, stage=stage, lease_token=None,
        lease_expires_at=None,
        next_poll_at=timezone.now() + timedelta(seconds=10),
        poll_failures=job.poll_failures + int(failed),
    )


def finish_error(
    job, code: str, detail: str, *, unknown=False, retryable=False,
) -> None:
    from w_craft_back.credits.video import release_video_generation
    with transaction.atomic():
        update_owned(
            job.pk, job.lease_token,
            status="outcome_unknown" if unknown else "failed",
            stage="outcome_unknown" if unknown else "failed",
            error_code=code, error_detail=detail[:500],
            error_retryable=retryable and not unknown,
            lease_token=None, lease_expires_at=None, next_poll_at=None,
            completed_at=timezone.now(),
        )
        # Once an external operation exists, a local polling/download failure
        # is not proof that no provider charge occurred. Keep it for settlement.
        if not unknown and not job.operation_id:
            release_video_generation(job, reason=code)


def recover_stale_video_jobs(*, limit=100) -> int:
    now = timezone.now()
    count = 0
    with transaction.atomic():
        jobs = VideoGenerationJob.objects.select_for_update(skip_locked=True).filter(
            status="processing", lease_token__isnull=False, lease_expires_at__lte=now,
        ).order_by("created_at")[:limit]
        for job in jobs:
            unknown = bool(job.provider_started_at and not job.operation_id)
            stage = "polling" if job.operation_id else "queued"
            detail = (
                "Provider may have accepted this request. "
                "It will not be submitted again."
            )
            VideoGenerationJob.objects.filter(
                pk=job.pk, lease_token=job.lease_token,
            ).update(
                status="outcome_unknown" if unknown else "queued",
                stage="outcome_unknown" if unknown else stage,
                error_code="VIDEO_OUTCOME_UNKNOWN" if unknown else "",
                error_detail=detail if unknown else "",
                error_retryable=False, lease_token=None, lease_expires_at=None,
                next_poll_at=None, completed_at=now if unknown else None,
                updated_at=now,
            )
            count += 1
    return count
