"""Durable lifecycle operations for poster generation jobs."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from w_craft_back.movie.poster.errors import PosterError
from w_craft_back.movie.poster.models import (
    PosterGenerationJob,
    PosterJobStatus,
    ProjectPosterStatus,
)
from w_craft_back.credits.services import release_generation


@transaction.atomic
def recover_stale_poster_jobs(*, limit: int = 100) -> dict[str, list[int]]:
    """Recover expired leases without discarding durable queued work."""
    now = timezone.now()
    batch_limit = max(1, min(int(limit), 1000))
    jobs = list(
        PosterGenerationJob.objects.select_for_update()
        .filter(
            status=PosterJobStatus.PROCESSING,
            lease_expires_at__lte=now,
        )
        .order_by("lease_expires_at")[:batch_limit]
    )
    requeued: list[int] = []
    failed: list[int] = []
    for job in jobs:
        if job.provider_started_at is not None:
            job.status = PosterJobStatus.FAILED
            job.error_code = "PROVIDER_OUTCOME_UNKNOWN"
            job.error_message = "Poster provider outcome is unknown after lease expiry"
            job.error_http_status = 503
            job.completed_at = now
            failed.append(job.pk)
        elif job.attempts >= job.max_attempts:
            job.status = PosterJobStatus.FAILED
            job.error_code = "MAX_ATTEMPTS_EXCEEDED"
            job.error_message = "Poster generation retry limit reached"
            job.error_http_status = 503
            job.completed_at = now
            failed.append(job.pk)
        else:
            job.status = PosterJobStatus.QUEUED
            job.progress = 0
            job.error_code = ""
            job.error_message = ""
            job.error_http_status = None
            requeued.append(job.pk)
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
        if job.status == PosterJobStatus.FAILED:
            release_generation(
                domain="poster",
                job_id=str(job.id),
                reason=job.error_code,
            )
    return {"requeued": requeued, "failed": failed}


@transaction.atomic
def heartbeat_poster_job(job: PosterGenerationJob) -> bool:
    """Refresh an owned lease and fail closed when its fence was lost."""
    from datetime import timedelta

    from w_craft_back.movie.poster.generation_guard import job_lease_seconds

    locked = PosterGenerationJob.objects.select_for_update().get(pk=job.pk)
    if (
        locked.status != PosterJobStatus.PROCESSING
        or locked.lease_token is None
        or locked.lease_token != job.lease_token
    ):
        return False
    now = timezone.now()
    locked.heartbeat_at = now
    locked.lease_expires_at = now + timedelta(seconds=job_lease_seconds())
    locked.save(update_fields=["heartbeat_at", "lease_expires_at", "updated_at"])
    return True


@transaction.atomic
def request_poster_cancellation(job_id: int) -> PosterGenerationJob:
    """Cancel queued poster work before provider execution starts."""
    job = (
        PosterGenerationJob.objects.select_for_update()
        .select_related("poster")
        .get(pk=job_id)
    )
    if job.status in (
        PosterJobStatus.CANCELLED,
        PosterJobStatus.CANCELLATION_REQUESTED,
    ):
        return job
    if job.status != PosterJobStatus.QUEUED:
        raise PosterError(
            "Poster generation can only be cancelled while it is queued.",
            code="POSTER_GENERATION_ALREADY_STARTED",
            http_status=409,
        )
    now = timezone.now()
    job.status = PosterJobStatus.CANCELLED
    job.progress = 0
    job.cancellation_requested_at = now
    job.completed_at = now
    job.lease_token = None
    job.lease_expires_at = None
    job.save()
    poster = job.poster
    poster.status = (
        ProjectPosterStatus.READY
        if poster.selected_variant_id
        else ProjectPosterStatus.EMPTY
    )
    poster.save(update_fields=["status", "updated_at"])
    release_generation(
        domain="poster",
        job_id=str(job.id),
        reason="cancelled_before_provider_start",
    )
    return job


@transaction.atomic
def retry_poster_job(
    original: PosterGenerationJob,
    *,
    actor,
) -> PosterGenerationJob:
    """Create or reuse a guarded retry from the durable request snapshot."""
    from w_craft_back.movie.poster.services import enqueue_generation_job

    locked = PosterGenerationJob.objects.select_for_update().get(pk=original.pk)
    if locked.status in (PosterJobStatus.QUEUED, PosterJobStatus.PROCESSING):
        raise PosterError(
            "An active poster job cannot be retried",
            code="POSTER_JOB_ACTIVE",
            http_status=409,
        )
    if locked.error_code == "PROVIDER_OUTCOME_UNKNOWN":
        raise PosterError(
            "Poster provider outcome is unknown; retry is unsafe",
            code="POSTER_PROVIDER_OUTCOME_UNKNOWN",
            http_status=409,
        )
    if (
        locked.status == PosterJobStatus.CANCELLATION_REQUESTED
        and locked.provider_started_at is not None
    ):
        raise PosterError(
            "Poster provider outcome is still unknown; retry is unsafe",
            code="POSTER_PROVIDER_OUTCOME_UNKNOWN",
            http_status=409,
        )

    existing_retry = locked.retries.order_by("created_at").first()
    if existing_retry is not None:
        return existing_retry

    retry_number = locked.retries.count() + 1
    _, retried, created = enqueue_generation_job(
        project=locked.project,
        user=actor,
        prompt=locked.prompt,
        style=locked.style,
        format=locked.format,
        operation=locked.operation,
        idempotency_key=f"retry:{locked.pk}:{retry_number}",
        request_hash=locked.request_hash,
        requested_model=locked.requested_model,
        routing_mode=str(
            (locked.provider_snapshot or {}).get("routingMode") or "manual"
        ),
        reference_storage_key=locked.reference_storage_key,
        reference_mime_type=locked.reference_mime_type,
        reference_image_url=locked.reference_image_url,
        reference_asset=locked.reference_asset,
        source_variant=locked.source_variant,
    )
    if created:
        retried.retry_of = locked
        retried.negative_prompt = locked.negative_prompt
        retried.max_attempts = locked.max_attempts
        retried.save(
            update_fields=(
                "retry_of",
                "negative_prompt",
                "max_attempts",
                "updated_at",
            )
        )
    return retried
