"""Durable transaction boundaries for character image generation jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterGenerationJob,
    CharacterGenerationGuard,
    CharacterImageType,
    GenerationJobStatus,
    GenerationJobType,
    StudioCharacter,
)
from w_craft_back.character_studio.services.errors import (
    ConflictError,
    GenerationBudgetExceededError,
    GenerationConcurrencyLimitError,
    IdempotencyKeyRequiredError,
    NotFoundError,
    ValidationError,
)
from w_craft_back.movie.project.models import Project


DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600
LEASE_GRACE_SECONDS = 30
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
TERMINAL_STATUSES = {
    GenerationJobStatus.COMPLETED,
    GenerationJobStatus.FAILED,
    GenerationJobStatus.CANCELLED,
    GenerationJobStatus.CANCELLATION_REQUESTED,
}
IMAGE_GENERATION_JOB_TYPES = tuple(
    value
    for value in GenerationJobType.values
    if value != GenerationJobType.MODEL3D_RECONSTRUCTION
)
ACTIVE_JOB_STATUSES = (
    GenerationJobStatus.QUEUED,
    GenerationJobStatus.PROCESSING,
)
DEFAULT_MAX_ACTIVE_GLOBAL = 4
DEFAULT_MAX_ACTIVE_PER_PROJECT = 2
DEFAULT_DAILY_BUDGET_PER_USER = 50
DEFAULT_DAILY_BUDGET_PER_PROJECT = 100


@dataclass(frozen=True)
class JobLease:
    job_id: uuid.UUID
    token: uuid.UUID
    timeout_seconds: int


def configured_timeout_seconds() -> int:
    raw = os.getenv(
        "CHARACTER_STUDIO_PROVIDER_TIMEOUT_SECONDS",
        str(DEFAULT_TIMEOUT_SECONDS),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    return max(5, min(value, MAX_TIMEOUT_SECONDS))


def _configured_value(name: str, default: object = None) -> object:
    value = getattr(settings, name, None)
    if value is None:
        value = os.getenv(name)
    return default if value in (None, "") else value


def _configured_positive_int(name: str, default: int) -> int:
    try:
        value = int(_configured_value(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def max_active_jobs_global() -> int:
    return _configured_positive_int(
        "CHARACTER_STUDIO_MAX_ACTIVE_GLOBAL", DEFAULT_MAX_ACTIVE_GLOBAL
    )


def max_active_jobs_per_project() -> int:
    return _configured_positive_int(
        "CHARACTER_STUDIO_MAX_ACTIVE_PER_PROJECT", DEFAULT_MAX_ACTIVE_PER_PROJECT
    )


def daily_budget_per_user() -> int:
    return _configured_positive_int(
        "CHARACTER_STUDIO_DAILY_BUDGET_PER_USER", DEFAULT_DAILY_BUDGET_PER_USER
    )


def daily_budget_per_project() -> int:
    return _configured_positive_int(
        "CHARACTER_STUDIO_DAILY_BUDGET_PER_PROJECT",
        DEFAULT_DAILY_BUDGET_PER_PROJECT,
    )


def estimated_cost_per_call(provider: str) -> Decimal | None:
    if provider.strip().lower() == "mock":
        return Decimal("0")
    raw = _configured_value("CHARACTER_STUDIO_ESTIMATED_COST_USD_PER_CALL")
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _clean_payload(payload: dict | None) -> dict:
    cleaned = dict(payload or {})
    for key in (
        "_idempotency_key",
        "token",
        "token_user",
        "user_key",
        "key",
    ):
        cleaned.pop(key, None)
    return cleaned


def _request_hash(
    *,
    character,
    job_type,
    region,
    variant_count,
    request_payload,
    compiled,
    provider_operation,
    provider,
) -> str:
    value = {
        "character_id": str(character.character_id),
        "job_type": str(job_type),
        "region": str(region),
        "variant_count": int(variant_count),
        "request_payload": _clean_payload(request_payload),
        "compiled": {
            "positive_prompt": compiled.get("positive_prompt", ""),
            "negative_prompt": compiled.get("negative_prompt", ""),
            "edit_instruction": compiled.get("edit_instruction", ""),
            "metadata": compiled.get("metadata", {}),
        },
        "provider_operation": provider_operation,
        "provider": provider,
        "policy_version": 1,
    }
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_name(project, actor) -> str:
    project_settings = (
        project.generation_settings
        if isinstance(project.generation_settings, dict)
        else {}
    )
    project_preference = (
        project_settings.get("image_generation_model")
        or project_settings.get("provider")
        or ""
    )
    actor_preference = ""
    try:
        django_user = getattr(actor, "user", None)
        profile = getattr(django_user, "profile", None) if django_user else None
        actor_preference = (
            getattr(profile, "image_generation_model", "") or ""
        ).strip()
    except Exception:  # noqa: BLE001 - preference lookup is best-effort
        actor_preference = ""
    return (
        str(project_preference).strip()
        or actor_preference
        or os.getenv("CHARACTER_STUDIO_IMAGE_PROVIDER")
        or "mock"
    )[:100]


def validate_idempotency_key(value) -> str:
    key = str(value or "").strip()
    if key and not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise ValidationError(
            "Idempotency-Key must be 1-128 characters using letters, "
            "numbers, '.', '_', ':' or '-'."
        )
    return key


def require_idempotency_key(value) -> str:
    key = validate_idempotency_key(value)
    if not key:
        raise IdempotencyKeyRequiredError()
    return key


def _clear_lease(job) -> None:
    job.lease_token = None
    job.lease_expires_at = None


def _recover_locked(job, now) -> bool:
    if (
        job.status != GenerationJobStatus.PROCESSING
        or job.lease_expires_at is None
        or job.lease_expires_at > now
    ):
        return False
    if job.provider_started_at is not None:
        job.status = GenerationJobStatus.FAILED
        job.error_code = "PROVIDER_OUTCOME_UNKNOWN"
        job.error_message = (
            "The worker lease expired after the provider call started. "
            "Automatic retry is disabled to prevent a duplicate paid call."
        )
        job.failed_at = now
        _clear_lease(job)
        job.save()
        return True
    if job.attempts >= job.max_attempts:
        job.status = GenerationJobStatus.FAILED
        job.error_code = "MAX_ATTEMPTS_EXCEEDED"
        job.error_message = "Generation worker exhausted its safe retry attempts."
        job.failed_at = now
        _clear_lease(job)
        job.save()
        return True
    job.status = GenerationJobStatus.QUEUED
    job.progress = 0
    job.error_code = ""
    job.error_message = ""
    _clear_lease(job)
    job.save()
    return True


def _usage_snapshot(
    actor: UserKey,
    project: Project,
    now,
) -> dict[str, int]:
    paid_jobs = (
        CharacterGenerationJob.objects.filter(
            created_at__gte=now - timedelta(hours=24),
            job_type__in=IMAGE_GENERATION_JOB_TYPES,
        )
        .exclude(provider__iexact="mock")
        .filter(
            Q(provider_started_at__isnull=False)
            | Q(status__in=ACTIVE_JOB_STATUSES)
        )
    )
    return {
        "user": paid_jobs.filter(actor=actor).count(),
        "project": paid_jobs.filter(project=project).count(),
    }


def build_generation_preview(
    *,
    actor: UserKey,
    character: StudioCharacter,
    image_types: list[str],
) -> dict[str, object]:
    normalized_types: list[str] = []
    for raw_image_type in image_types:
        image_type = str(raw_image_type or "").strip()
        if image_type not in CharacterImageType.values:
            raise ValidationError(f"Unknown image_type: {image_type}.")
        if image_type not in normalized_types:
            normalized_types.append(image_type)
    if not normalized_types:
        raise ValidationError("At least one image_type is required.")

    provider = _provider_name(character.project, actor)
    now = timezone.now()
    usage = _usage_snapshot(actor, character.project, now)
    active_jobs = CharacterGenerationJob.objects.filter(
        job_type__in=IMAGE_GENERATION_JOB_TYPES,
        status=GenerationJobStatus.PROCESSING,
    )
    call_count = len(normalized_types)
    unit_cost = estimated_cost_per_call(provider)
    estimated_cost = (
        None if unit_cost is None else format(unit_cost * call_count, "f")
    )
    return {
        "provider": provider,
        "mode": "offline" if provider.strip().lower() == "mock" else "paid",
        "image_types": normalized_types,
        "provider_call_count": call_count,
        "estimated_cost_usd": estimated_cost,
        "budgets": {
            "user": {
                "used": usage["user"],
                "limit": daily_budget_per_user(),
            },
            "project": {
                "used": usage["project"],
                "limit": daily_budget_per_project(),
            },
        },
        "concurrency": {
            "global": {
                "active": active_jobs.count(),
                "limit": max_active_jobs_global(),
            },
            "project": {
                "active": active_jobs.filter(project=character.project).count(),
                "limit": max_active_jobs_per_project(),
            },
        },
    }


def _lock_generation_scope(actor: UserKey, project: Project) -> None:
    guard, _ = CharacterGenerationGuard.objects.get_or_create(key="global")
    CharacterGenerationGuard.objects.select_for_update().get(pk=guard.pk)
    Project.objects.select_for_update().get(pk=project.pk)
    UserKey.objects.select_for_update().get(pk=actor.pk)


def _recover_stale_active_jobs(now) -> None:
    stale_jobs = CharacterGenerationJob.objects.select_for_update().filter(
        job_type__in=IMAGE_GENERATION_JOB_TYPES,
        status=GenerationJobStatus.PROCESSING,
        lease_expires_at__lte=now,
    )
    for stale_job in stale_jobs:
        _recover_locked(stale_job, now)


def _enforce_generation_limits(
    *,
    actor: UserKey,
    project: Project,
    provider: str,
    now,
) -> None:
    _lock_generation_scope(actor, project)
    _recover_stale_active_jobs(now)
    active_jobs = CharacterGenerationJob.objects.filter(
        job_type__in=IMAGE_GENERATION_JOB_TYPES,
        status=GenerationJobStatus.PROCESSING,
    )
    if active_jobs.count() >= max_active_jobs_global():
        raise GenerationConcurrencyLimitError(
            "Global Character Studio concurrency limit reached."
        )
    if active_jobs.filter(project=project).count() >= max_active_jobs_per_project():
        raise GenerationConcurrencyLimitError(
            "Project Character Studio concurrency limit reached."
        )
    if provider.strip().lower() == "mock":
        return

    usage = _usage_snapshot(actor, project, now)
    if usage["user"] >= daily_budget_per_user():
        raise GenerationBudgetExceededError(
            "User Character Studio daily budget is exhausted."
        )
    if usage["project"] >= daily_budget_per_project():
        raise GenerationBudgetExceededError(
            "Project Character Studio daily budget is exhausted."
        )


@transaction.atomic
def enqueue_job(
    *,
    actor,
    character,
    job_type,
    region,
    variant_count,
    request_payload,
    compiled,
    provider_operation,
) -> CharacterGenerationJob:
    """Create or reuse a queued job while holding only a short character lock."""
    locked_character = (
        StudioCharacter.objects.select_for_update(of=("self",))
        .select_related("project", "user")
        .get(pk=character.pk)
    )
    payload = _clean_payload(request_payload)
    idempotency_key = validate_idempotency_key(
        (request_payload or {}).get("_idempotency_key")
    )
    provider = _provider_name(locked_character.project, actor)
    request_hash = _request_hash(
        character=locked_character,
        job_type=job_type,
        region=region,
        variant_count=variant_count,
        request_payload=payload,
        compiled=compiled,
        provider_operation=provider_operation,
        provider=provider,
    )
    if idempotency_key:
        existing = CharacterGenerationJob.objects.filter(
            project=locked_character.project,
            actor=actor,
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ConflictError(
                    "Idempotency-Key was already used for a different request."
                )
            return existing

    now = timezone.now()
    active_jobs = CharacterGenerationJob.objects.select_for_update().filter(
        character=locked_character,
        request_hash=request_hash,
        status__in=[
            GenerationJobStatus.QUEUED,
            GenerationJobStatus.PROCESSING,
        ],
    )
    for active_job in active_jobs:
        _recover_locked(active_job, now)
        active_job.refresh_from_db()
        if active_job.status in (
            GenerationJobStatus.QUEUED,
            GenerationJobStatus.PROCESSING,
        ):
            return active_job
        if (
            not idempotency_key
            and active_job.error_code == "PROVIDER_OUTCOME_UNKNOWN"
        ):
            return active_job

    if not idempotency_key:
        ambiguous_job = (
            CharacterGenerationJob.objects.filter(
                character=locked_character,
                request_hash=request_hash,
                status=GenerationJobStatus.FAILED,
                error_code="PROVIDER_OUTCOME_UNKNOWN",
            )
            .order_by("-failed_at", "-created_at")
            .first()
        )
        if ambiguous_job is not None:
            return ambiguous_job
    _enforce_generation_limits(
        actor=actor,
        project=locked_character.project,
        provider=provider,
        now=now,
    )

    image_type = payload.get("image_type")
    if image_type:
        conflicting_job = (
            CharacterGenerationJob.objects.select_for_update()
            .filter(
                character=locked_character,
                status__in=[
                    GenerationJobStatus.QUEUED,
                    GenerationJobStatus.PROCESSING,
                ],
                request_payload__image_type=image_type,
            )
            .exclude(request_hash=request_hash)
            .first()
        )
        if conflicting_job is not None:
            _recover_locked(conflicting_job, now)
            conflicting_job.refresh_from_db()
            if conflicting_job.status in (
                GenerationJobStatus.QUEUED,
                GenerationJobStatus.PROCESSING,
            ):
                raise ConflictError(
                    "Generation already running for this image type."
                )

    values = {
        "character": locked_character,
        "project": locked_character.project,
        "user": locked_character.user,
        "actor": actor,
        "job_type": job_type,
        "status": GenerationJobStatus.QUEUED,
        "region": region,
        "variant_count": variant_count,
        "request_payload": payload,
        "request_hash": request_hash,
        "idempotency_key": idempotency_key,
        "compiled_prompt": compiled.get("positive_prompt", ""),
        "negative_prompt": compiled.get("negative_prompt", ""),
        "edit_instruction": compiled.get("edit_instruction", ""),
        "compiled_metadata": compiled.get("metadata", {}),
        "preserve_options": compiled.get("metadata", {}).get("preserve", {}),
        "provider": provider,
        "provider_operation": provider_operation,
        "timeout_seconds": configured_timeout_seconds(),
    }
    if not idempotency_key:
        return CharacterGenerationJob.objects.create(**values)
    try:
        with transaction.atomic():
            return CharacterGenerationJob.objects.create(**values)
    except IntegrityError:
        existing = CharacterGenerationJob.objects.get(
            project=locked_character.project,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        if existing.request_hash != request_hash:
            raise ConflictError(
                "Idempotency-Key was already used for a different request."
            )
        return existing


@transaction.atomic
def claim_job(job_id) -> JobLease | None:
    """Claim a queued job without retaining the transaction for provider I/O."""
    snapshot = CharacterGenerationJob.objects.select_related(
        "actor", "project"
    ).get(job_id=job_id)
    if snapshot.job_type in IMAGE_GENERATION_JOB_TYPES and snapshot.actor_id:
        _lock_generation_scope(snapshot.actor, snapshot.project)

    job = CharacterGenerationJob.objects.select_for_update().get(job_id=job_id)
    now = timezone.now()
    _recover_locked(job, now)
    job.refresh_from_db()
    if job.status in TERMINAL_STATUSES:
        return None
    if job.status == GenerationJobStatus.PROCESSING:
        return None
    if job.actor_id is None:
        job.status = GenerationJobStatus.FAILED
        job.error_code = "GENERATION_ACTOR_MISSING"
        job.error_message = "The generation actor no longer exists."
        job.failed_at = now
        job.save()
        return None
    if job.attempts >= job.max_attempts:
        job.status = GenerationJobStatus.FAILED
        job.error_code = "MAX_ATTEMPTS_EXCEEDED"
        job.error_message = "Generation worker exhausted its safe retry attempts."
        job.failed_at = now
        job.save()
        return None

    if job.job_type in IMAGE_GENERATION_JOB_TYPES:
        _recover_stale_active_jobs(now)
        active_jobs = CharacterGenerationJob.objects.filter(
            job_type__in=IMAGE_GENERATION_JOB_TYPES,
            status=GenerationJobStatus.PROCESSING,
        ).exclude(pk=job.pk)
        if active_jobs.count() >= max_active_jobs_global():
            return None
        if (
            active_jobs.filter(project_id=job.project_id).count()
            >= max_active_jobs_per_project()
        ):
            return None

    token = uuid.uuid4()
    job.status = GenerationJobStatus.PROCESSING
    job.progress = 10
    job.attempts += 1
    job.lease_token = token
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(
        seconds=job.timeout_seconds + LEASE_GRACE_SECONDS
    )
    job.provider_started_at = None
    if job.started_at is None:
        job.started_at = now
    job.error_code = ""
    job.error_message = ""
    job.save()
    return JobLease(job.job_id, token, job.timeout_seconds)


@transaction.atomic
def mark_provider_started(lease: JobLease) -> bool:
    job = CharacterGenerationJob.objects.select_for_update().get(
        job_id=lease.job_id
    )
    if (
        job.status != GenerationJobStatus.PROCESSING
        or job.lease_token != lease.token
    ):
        return False
    now = timezone.now()
    job.provider_started_at = now
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(
        seconds=lease.timeout_seconds + LEASE_GRACE_SECONDS
    )
    job.save()
    return True


@transaction.atomic
def heartbeat_job(lease: JobLease) -> bool:
    job = CharacterGenerationJob.objects.select_for_update().get(
        job_id=lease.job_id
    )
    if (
        job.status != GenerationJobStatus.PROCESSING
        or job.lease_token != lease.token
    ):
        return False
    now = timezone.now()
    job.heartbeat_at = now
    job.lease_expires_at = now + timedelta(
        seconds=lease.timeout_seconds + LEASE_GRACE_SECONDS
    )
    job.save(update_fields=["heartbeat_at", "lease_expires_at", "updated_at"])
    return True


@transaction.atomic
def fail_job(lease: JobLease, *, error_code: str, error_message: str):
    job = CharacterGenerationJob.objects.select_for_update().get(
        job_id=lease.job_id
    )
    if (
        job.status != GenerationJobStatus.PROCESSING
        or job.lease_token != lease.token
    ):
        return job
    job.status = GenerationJobStatus.FAILED
    job.error_code = (error_code or "GENERATION_FAILED")[:100]
    job.error_message = str(error_message or "Generation failed.")
    job.failed_at = timezone.now()
    job.progress = 0
    _clear_lease(job)
    job.save()
    return job


@transaction.atomic
def recover_stale_jobs(*, limit: int = 100) -> dict:
    """Return recoverable image jobs, including queued crash-gap work."""
    now = timezone.now()
    batch_limit = max(1, min(int(limit), 1000))
    expired_jobs = list(
        CharacterGenerationJob.objects.select_for_update()
        .filter(
            job_type__in=GenerationJobType.values,
            status=GenerationJobStatus.PROCESSING,
            lease_expires_at__isnull=False,
            lease_expires_at__lte=now,
        )
        .order_by("lease_expires_at")[:batch_limit]
    )
    ready = []
    failed = []
    for job in expired_jobs:
        _recover_locked(job, now)
        job.refresh_from_db()
        target = ready if job.status == GenerationJobStatus.QUEUED else failed
        target.append(str(job.job_id))

    remaining = batch_limit - len(expired_jobs)
    if remaining > 0:
        queued_jobs = (
            CharacterGenerationJob.objects.select_for_update()
            .filter(
                job_type__in=GenerationJobType.values,
                status=GenerationJobStatus.QUEUED,
            )
            .exclude(job_id__in=ready)
            .order_by("created_at")[:remaining]
        )
        ready.extend(str(job.job_id) for job in queued_jobs)
    return {"requeued": ready, "failed": failed}


def list_character_jobs(*, actor, project_id, character_id, limit: int = 50):
    """Return recent jobs scoped to a viewable project and character."""
    from w_craft_back.character_studio.services.permissions import (
        get_viewable_project,
    )

    get_viewable_project(actor, project_id)
    batch_limit = max(1, min(int(limit), 200))
    return list(
        CharacterGenerationJob.objects.filter(
            project_id=project_id,
            character_id=character_id,
        ).order_by("-created_at")[:batch_limit]
    )


@transaction.atomic
def request_job_cancellation(*, actor, job_id):
    """Fence an active job and record that cancellation was requested."""
    from w_craft_back.character_studio.services.permissions import (
        get_generation_project,
    )

    try:
        job = CharacterGenerationJob.objects.select_for_update().get(job_id=job_id)
    except CharacterGenerationJob.DoesNotExist as exc:
        raise NotFoundError("Generation job not found.") from exc
    get_generation_project(actor, job.project_id)
    if job.status in (
        GenerationJobStatus.QUEUED,
        GenerationJobStatus.PROCESSING,
    ):
        job.status = GenerationJobStatus.CANCELLATION_REQUESTED
        job.cancellation_requested_at = timezone.now()
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
    return job


@transaction.atomic
def retry_character_job(*, actor, job_id):
    """Create or reuse a safe retry from an immutable request snapshot."""
    from w_craft_back.character_studio.services.permissions import (
        get_generation_project,
    )

    try:
        original = (
            CharacterGenerationJob.objects.select_for_update()
            .select_related("character")
            .get(job_id=job_id)
        )
    except CharacterGenerationJob.DoesNotExist as exc:
        raise NotFoundError("Generation job not found.") from exc
    get_generation_project(actor, original.project_id)
    if original.status in (
        GenerationJobStatus.QUEUED,
        GenerationJobStatus.PROCESSING,
    ):
        raise ConflictError("An active generation job cannot be retried.")
    if original.error_code == "PROVIDER_OUTCOME_UNKNOWN":
        raise ConflictError("Provider outcome is unknown; retry is unsafe.")
    if (
        original.status == GenerationJobStatus.CANCELLATION_REQUESTED
        and original.provider_started_at is not None
    ):
        raise ConflictError("Provider outcome is still unknown; retry is unsafe.")

    existing_retry = original.retries.order_by("created_at").first()
    if existing_retry is not None:
        return existing_retry

    if original.job_type in IMAGE_GENERATION_JOB_TYPES:
        _enforce_generation_limits(
            actor=actor,
            project=original.project,
            provider=original.provider,
            now=timezone.now(),
        )

    if original.job_type == GenerationJobType.MODEL3D_RECONSTRUCTION:
        from w_craft_back.character_studio.services.model3d_reconstruction_service import (
            retry_reconstruction,
        )

        state = retry_reconstruction(original.character, actor=actor)
        retried_id = state.get("job_id")
        if not retried_id or str(retried_id) == str(original.job_id):
            raise ConflictError("3D reconstruction could not be retried.")
        retried = CharacterGenerationJob.objects.get(job_id=retried_id)
        retried.retry_of = original
        retried.save(update_fields=["retry_of", "updated_at"])
        return retried

    return CharacterGenerationJob.objects.create(
        character=original.character,
        project=original.project,
        user=original.user,
        actor=actor,
        retry_of=original,
        job_type=original.job_type,
        status=GenerationJobStatus.QUEUED,
        region=original.region,
        variant_count=original.variant_count,
        request_payload=dict(original.request_payload or {}),
        request_hash=original.request_hash,
        compiled_prompt=original.compiled_prompt,
        negative_prompt=original.negative_prompt,
        edit_instruction=original.edit_instruction,
        preserve_options=dict(original.preserve_options or {}),
        compiled_metadata=dict(original.compiled_metadata or {}),
        provider=original.provider,
        provider_operation=original.provider_operation,
        timeout_seconds=original.timeout_seconds,
        max_attempts=original.max_attempts,
    )
