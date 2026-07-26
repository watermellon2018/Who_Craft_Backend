"""Limits, idempotency helpers, and provider circuit state for poster calls."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from w_craft_back.movie.poster.errors import (
    IdempotencyKeyInvalid,
    IdempotencyKeyRequired,
    PosterProviderCircuitOpen,
)
from w_craft_back.movie.poster.models import PosterProviderCircuit

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def normalize_idempotency_key(value: object) -> str:
    """Validate the HTTP idempotency key without logging or storing payloads."""
    if not isinstance(value, str) or not value.strip():
        raise IdempotencyKeyRequired("Idempotency-Key header is required")
    key = value.strip()
    if len(key) > 128 or not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise IdempotencyKeyInvalid("Idempotency-Key header is invalid")
    return key


def request_fingerprint(payload: dict[str, Any], binary: bytes | None = None) -> str:
    """Return a stable hash for idempotency conflict detection."""
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    if binary is not None:
        digest.update(hashlib.sha256(binary).digest())
    return digest.hexdigest()


def max_active_jobs() -> int:
    return _positive_int("POSTER_MAX_ACTIVE_JOBS_PER_USER_PROJECT", 1)


def max_active_jobs_per_user() -> int:
    return _positive_int("POSTER_MAX_ACTIVE_JOBS_PER_USER", 2)


def job_lease_seconds() -> int:
    return provider_timeout_seconds() + _positive_int(
        "POSTER_JOB_LEASE_GRACE_SECONDS",
        30,
    )


def daily_quota() -> int:
    return _positive_int("POSTER_DAILY_QUOTA_PER_USER_PROJECT", 20)


def daily_quota_per_user() -> int:
    return _positive_int("POSTER_DAILY_QUOTA_PER_USER", 50)


def quota_window_start():
    return timezone.now() - timedelta(hours=24)


def provider_timeout_seconds() -> int:
    return _positive_int("POSTER_PROVIDER_TIMEOUT_SECONDS", 60)


def max_input_bytes() -> int:
    return _positive_int("POSTER_MAX_INPUT_BYTES", 10 * 1024 * 1024)


def max_output_bytes() -> int:
    return _positive_int("IMAGE_PROVIDER_MAX_OUTPUT_BYTES", 20 * 1024 * 1024)


def max_output_pixels() -> int:
    return _positive_int("IMAGE_PROVIDER_MAX_OUTPUT_PIXELS", 40_000_000)


def _circuit_threshold() -> int:
    return _positive_int("POSTER_CIRCUIT_FAILURE_THRESHOLD", 3)


def _circuit_cooldown_seconds() -> int:
    return _positive_int("POSTER_CIRCUIT_COOLDOWN_SECONDS", 120)


def provider_circuit_key(provider: object) -> str:
    name = str(getattr(provider, "name", provider.__class__.__name__))
    model = str(getattr(provider, "model_id", "") or "")
    raw = f"{name}:{model}" if model else name
    if len(raw) <= 255:
        return raw
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{raw[:238]}:{suffix}"


def ensure_provider_circuit_closed(provider_key: str) -> None:
    """Fail fast while open and atomically reserve one half-open probe."""
    with transaction.atomic():
        circuit = (
            PosterProviderCircuit.objects.select_for_update()
            .filter(provider_key=provider_key)
            .first()
        )
        if circuit is None or circuit.opened_until is None:
            return

        now = timezone.now()
        if circuit.opened_until > now:
            raise PosterProviderCircuitOpen(
                "Poster provider is temporarily unavailable"
            )

        # The cooldown elapsed. Keep the circuit closed to other callers while
        # this request probes the provider. Success clears the reservation;
        # failure reopens the normal cooldown in ``record_provider_failure``.
        circuit.opened_until = now + timedelta(
            seconds=provider_timeout_seconds() + 5
        )
        circuit.save(update_fields=["opened_until", "updated_at"])


def record_provider_success(provider_key: str) -> None:
    PosterProviderCircuit.objects.filter(provider_key=provider_key).update(
        failure_count=0,
        opened_until=None,
    )


def record_provider_failure(provider_key: str) -> None:
    """Open the shared DB-backed circuit after consecutive provider failures."""
    with transaction.atomic():
        circuit, _ = PosterProviderCircuit.objects.select_for_update().get_or_create(
            provider_key=provider_key,
        )
        circuit.failure_count += 1
        if circuit.failure_count >= _circuit_threshold():
            circuit.opened_until = timezone.now() + timedelta(
                seconds=_circuit_cooldown_seconds()
            )
        circuit.save(update_fields=["failure_count", "opened_until", "updated_at"])
