"""Safe public and provider errors for Sound Effects."""

from __future__ import annotations


class SoundEffectError(RuntimeError):
    code = "SOUND_EFFECT_ERROR"
    http_status = 400
    retryable = False

    def __init__(
        self,
        detail: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
        errors: dict | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code or self.code
        self.http_status = http_status or self.http_status
        self.retryable = self.retryable if retryable is None else retryable
        self.errors = errors


class SoundEffectProviderError(SoundEffectError):
    """Sanitized adapter error with paid-outcome settlement metadata."""

    code = "SOUND_EFFECT_PROVIDER_UNAVAILABLE"
    http_status = 503
    retryable = True

    def __init__(
        self,
        detail: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
        outcome_unknown: bool = False,
        cost_incurred: bool = False,
    ) -> None:
        super().__init__(
            detail,
            code=code,
            http_status=http_status,
            retryable=retryable,
        )
        self.outcome_unknown = outcome_unknown
        self.cost_incurred = cost_incurred


PUBLIC_PROVIDER_DETAILS = {
    "SOUND_EFFECT_PROVIDER_NOT_CONFIGURED": "Sound-effects provider is not configured.",
    "SOUND_EFFECT_PROVIDER_RATE_LIMITED": (
        "Sound-effects provider rate limit was reached."
    ),
    "SOUND_EFFECT_PROVIDER_TIMEOUT": "Sound-effects provider timed out.",
    "SOUND_EFFECT_PROVIDER_REJECTED": "Sound-effects provider rejected the request.",
    "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN": (
        "Sound-effects provider outcome is unknown."
    ),
    "SOUND_EFFECT_OUTPUT_INVALID": "Sound-effects provider returned invalid audio.",
    "SOUND_EFFECT_OUTPUT_TOO_LARGE": "Generated sound effect is too large.",
}


def public_provider_detail(code: str) -> str:
    return PUBLIC_PROVIDER_DETAILS.get(code, "Sound-effects provider is unavailable.")
