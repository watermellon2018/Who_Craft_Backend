"""Stable, safe public errors for the project-scoped Music Studio API."""

from __future__ import annotations

from rest_framework import status


_PUBLIC_PROVIDER_ERROR_DETAILS = {
    "MUSIC_PROVIDER_NOT_CONFIGURED": "Music provider is not configured.",
    "MUSIC_PROVIDER_RATE_LIMITED": "Music provider rate limit was reached.",
    "MUSIC_PROVIDER_TIMEOUT": "Music provider timed out.",
    "MUSIC_PROVIDER_REJECTED": "Music provider rejected the request.",
    "MUSIC_PROVIDER_OUTCOME_UNKNOWN": "Music provider outcome is unknown.",
    "MUSIC_REFERENCE_REJECTED": "Music provider rejected the audio reference.",
    "MUSIC_CAPABILITY_UNSUPPORTED": "Music provider does not support this request.",
    "MUSIC_OUTPUT_TOO_LARGE": "Generated audio exceeds the configured byte limit.",
}


def public_provider_error_detail(code: str) -> str | None:
    """Return a fixed public detail for provider-originated failures."""

    detail = _PUBLIC_PROVIDER_ERROR_DETAILS.get(code)
    if detail is not None:
        return detail
    if str(code).startswith("MUSIC_PROVIDER_"):
        return "Music provider is unavailable."
    return None


class MusicError(Exception):
    """Domain/API error whose public representation is safe to return."""

    code = "MUSIC_ERROR"
    http_status = status.HTTP_400_BAD_REQUEST
    retryable: bool | str = False

    def __init__(
        self,
        detail: str = "",
        *,
        code: str | None = None,
        errors: dict | None = None,
        current_version: int | None = None,
        retryable: bool | str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail or self.code
        if code is not None:
            self.code = code
        self.errors = errors
        self.current_version = current_version
        if retryable is not None:
            self.retryable = retryable
        if http_status is not None:
            self.http_status = http_status


class ValidationError(MusicError):
    code = "MUSIC_VALIDATION_ERROR"


class CapabilityUnsupported(MusicError):
    code = "MUSIC_CAPABILITY_UNSUPPORTED"


class ProjectNotFound(MusicError):
    code = "MUSIC_PROJECT_NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND


class TrackNotFound(MusicError):
    code = "MUSIC_TRACK_NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND


class SceneNotFound(MusicError):
    code = "MUSIC_SCENE_NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND


class ReferenceNotFound(MusicError):
    code = "MUSIC_REFERENCE_NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND


class ReferenceInvalid(MusicError):
    code = "MUSIC_REFERENCE_INVALID"


class ReferenceRightsRequired(MusicError):
    code = "MUSIC_REFERENCE_RIGHTS_REQUIRED"


class ReferenceRejected(MusicError):
    code = "MUSIC_REFERENCE_REJECTED"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY


class LyricsUnsupported(MusicError):
    code = "MUSIC_LYRICS_UNSUPPORTED"


class JobNotFound(MusicError):
    code = "MUSIC_JOB_NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND


class VariantNotFound(MusicError):
    code = "MUSIC_VARIANT_NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND


class PermissionDenied(MusicError):
    code = "MUSIC_PERMISSION_DENIED"
    http_status = status.HTTP_403_FORBIDDEN


class IdempotencyRequired(MusicError):
    code = "MUSIC_IDEMPOTENCY_REQUIRED"


class IdempotencyConflict(MusicError):
    code = "MUSIC_IDEMPOTENCY_CONFLICT"
    http_status = status.HTTP_409_CONFLICT


class VersionConflict(MusicError):
    code = "VERSION_CONFLICT"
    http_status = status.HTTP_409_CONFLICT

    def __init__(self, current_version: int) -> None:
        super().__init__(
            "The track changed after it was opened. Refresh and try again.",
            current_version=current_version,
        )


class GenerationConflict(MusicError):
    code = "MUSIC_GENERATION_CONFLICT"
    http_status = status.HTTP_409_CONFLICT
    retryable = True


class QuotaExceeded(MusicError):
    code = "MUSIC_QUOTA_EXCEEDED"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS


class ProviderNotConfigured(MusicError):
    code = "MUSIC_PROVIDER_NOT_CONFIGURED"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    retryable = True


class ProviderRateLimited(MusicError):
    code = "MUSIC_PROVIDER_RATE_LIMITED"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    retryable = True


class ProviderTimeout(MusicError):
    code = "MUSIC_PROVIDER_TIMEOUT"
    http_status = status.HTTP_504_GATEWAY_TIMEOUT
    retryable = True


class ProviderRejected(MusicError):
    code = "MUSIC_PROVIDER_REJECTED"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY


class ProviderOutcomeUnknown(MusicError):
    code = "MUSIC_PROVIDER_OUTCOME_UNKNOWN"
    http_status = status.HTTP_502_BAD_GATEWAY
    retryable = "manual"


class OutputInvalid(MusicError):
    code = "MUSIC_OUTPUT_INVALID"
    http_status = status.HTTP_502_BAD_GATEWAY
    retryable = True


class OutputTooLarge(MusicError):
    code = "MUSIC_OUTPUT_TOO_LARGE"
    http_status = status.HTTP_502_BAD_GATEWAY


class MaxAttemptsExceeded(MusicError):
    code = "MUSIC_MAX_ATTEMPTS_EXCEEDED"
    http_status = status.HTTP_409_CONFLICT


class CannotCancel(MusicError):
    code = "MUSIC_CANNOT_CANCEL"
    http_status = status.HTTP_409_CONFLICT


def validation_error(errors: dict) -> ValidationError:
    """Build the canonical serializer-validation error."""

    return ValidationError("Validation failed.", errors=errors)
