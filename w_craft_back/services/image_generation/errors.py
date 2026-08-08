"""Uniform error type for image generation + translation from upstream errors.

The view layer should only ever see :class:`ImageProviderError`. Providers
catch their native SDK exceptions and call :func:`map_to_provider_error` to
turn them into this shape.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _fragment_metadata(value: str | None) -> tuple[int, str]:
    raw = (value or "").encode("utf-8", errors="replace")
    return len(raw), hashlib.sha256(raw).hexdigest()


class ImageProviderError(Exception):
    """Structured error from the image-generation pipeline.

    Carries a stable public response and non-reversible metadata for upstream
    diagnostics. Raw provider response fragments are deliberately discarded.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 502,
        provider_status: int | None = None,
        provider_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.provider_status = provider_status
        self.provider_body_length, self.provider_body_hash = _fragment_metadata(
            provider_body
        )
        self.provider_body = None


# Public error codes — kept stable for the FE.
CODE_NOT_CONFIGURED = "IMAGE_PROVIDER_NOT_CONFIGURED"
CODE_FORBIDDEN = "IMAGE_PROVIDER_FORBIDDEN"
CODE_UNAVAILABLE = "IMAGE_PROVIDER_UNAVAILABLE"
CODE_BLOCKED = "IMAGE_PROVIDER_BLOCKED"
CODE_BAD_RESPONSE = "IMAGE_PROVIDER_BAD_RESPONSE"
CODE_ERROR = "IMAGE_PROVIDER_ERROR"
CODE_EDIT_NOT_SUPPORTED = "IMAGE_PROVIDER_EDIT_NOT_SUPPORTED"
CODE_IMAGE_INPUT_NOT_SUPPORTED = "MODEL_DOES_NOT_SUPPORT_IMAGE_INPUT"
CODE_MODEL_UNKNOWN = "IMAGE_MODEL_UNKNOWN"


def _gemini_kind_to_provider_error(exc: Any) -> ImageProviderError:
    """Translate a ``GeminiImageError`` (movie/poster/gemini_image.py) into the
    public :class:`ImageProviderError`.
    """
    kind = getattr(exc, "kind", "error")
    provider_status = getattr(exc, "provider_status", None)
    provider_body = getattr(exc, "provider_body", None)

    if kind == "not_configured":
        return ImageProviderError(
            code=CODE_NOT_CONFIGURED,
            message=(
                "Не настроен API ключ провайдера генерации изображений. "
                "Задайте GEMINI_API_KEY в окружении."
            ),
            http_status=503,
        )
    if kind == "forbidden":
        return ImageProviderError(
            code=CODE_FORBIDDEN,
            message=(
                "Провайдер генерации изображений отклонил запрос. "
                "Проверьте GEMINI_API_KEY, доступ проекта к Imagen API и квоты."
            ),
            http_status=502,
            provider_status=provider_status,
            provider_body=provider_body,
        )
    if kind == "unavailable":
        return ImageProviderError(
            code=CODE_UNAVAILABLE,
            message="Провайдер генерации изображений недоступен. Попробуйте позже.",
            http_status=503,
            provider_status=provider_status,
            provider_body=provider_body,
        )
    if kind == "blocked":
        return ImageProviderError(
            code=CODE_BLOCKED,
            message=(
                "Промпт был заблокирован фильтрами безопасности. "
                "Переформулируйте описание."
            ),
            http_status=400,
            provider_status=provider_status,
            provider_body=provider_body,
        )
    if kind in ("empty", "bad_response"):
        return ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Провайдер генерации изображений вернул некорректный ответ.",
            http_status=502,
            provider_status=provider_status,
            provider_body=provider_body,
        )
    return ImageProviderError(
        code=CODE_ERROR,
        message=f"Провайдер генерации изображений вернул ошибку (HTTP {provider_status}).",
        http_status=502,
        provider_status=provider_status,
        provider_body=provider_body,
    )


_SAFETY_HINTS = (
    "safety",
    "content policy",
    "violat",
    "harmful",
    "blocked",
    "prohibit",
)


def _looks_like_safety(message: str) -> bool:
    lowered = (message or "").lower()
    return any(hint in lowered for hint in _SAFETY_HINTS)


def map_to_provider_error(exc: BaseException) -> ImageProviderError:
    """Translate any upstream exception into :class:`ImageProviderError`.

    Already-:class:`ImageProviderError` exceptions pass through unchanged so
    callers can ``raise map_to_provider_error(exc) from exc`` blindly.
    """
    if isinstance(exc, ImageProviderError):
        return exc

    # Native Gemini REST error (movie/poster/gemini_image.GeminiImageError).
    if exc.__class__.__name__ == "GeminiImageError":
        return _gemini_kind_to_provider_error(exc)

    # LiteLLM exceptions — imported lazily so the module loads without litellm.
    try:  # pragma: no cover - exercised only when litellm is installed
        from litellm import exceptions as litellm_exc  # type: ignore
    except Exception:  # noqa: BLE001 — litellm may be absent in tests
        litellm_exc = None  # type: ignore

    if litellm_exc is not None:
        if isinstance(exc, getattr(litellm_exc, "AuthenticationError", ())):
            return ImageProviderError(
                code=CODE_FORBIDDEN,
                message="Провайдер отклонил запрос: проверьте API-ключ выбранной модели.",
                http_status=502,
                provider_body=str(exc)[:1000],
            )
        if isinstance(exc, getattr(litellm_exc, "PermissionDeniedError", ())):
            return ImageProviderError(
                code=CODE_FORBIDDEN,
                message="Доступ к выбранной модели запрещён провайдером.",
                http_status=502,
                provider_body=str(exc)[:1000],
            )
        if isinstance(exc, getattr(litellm_exc, "RateLimitError", ())):
            return ImageProviderError(
                code=CODE_UNAVAILABLE,
                message="Провайдер вернул rate limit. Попробуйте позже.",
                http_status=503,
                provider_body=str(exc)[:1000],
            )
        if isinstance(exc, getattr(litellm_exc, "ServiceUnavailableError", ())):
            return ImageProviderError(
                code=CODE_UNAVAILABLE,
                message="Провайдер временно недоступен.",
                http_status=503,
                provider_body=str(exc)[:1000],
            )
        if isinstance(exc, getattr(litellm_exc, "Timeout", ())):
            return ImageProviderError(
                code=CODE_UNAVAILABLE,
                message="Таймаут при обращении к провайдеру.",
                http_status=504,
                provider_body=str(exc)[:1000],
            )
        if isinstance(exc, getattr(litellm_exc, "APIConnectionError", ())):
            return ImageProviderError(
                code=CODE_UNAVAILABLE,
                message="Не удалось соединиться с провайдером.",
                http_status=503,
                provider_body=str(exc)[:1000],
            )
        content_policy_cls = getattr(litellm_exc, "ContentPolicyViolationError", None)
        if content_policy_cls is not None and isinstance(exc, content_policy_cls):
            return ImageProviderError(
                code=CODE_BLOCKED,
                message=(
                    "Промпт заблокирован фильтрами безопасности провайдера. "
                    "Переформулируйте описание."
                ),
                http_status=400,
                provider_body=str(exc)[:1000],
            )
        bad_request_cls = getattr(litellm_exc, "BadRequestError", None)
        if bad_request_cls is not None and isinstance(exc, bad_request_cls):
            msg = str(exc)
            if _looks_like_safety(msg):
                return ImageProviderError(
                    code=CODE_BLOCKED,
                    message=(
                        "Промпт заблокирован фильтрами безопасности провайдера. "
                        "Переформулируйте описание."
                    ),
                    http_status=400,
                    provider_body=msg[:1000],
                )
            return ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="Провайдер отклонил запрос как некорректный.",
                http_status=400,
                provider_body=msg[:1000],
            )

    logger.error(
        "image_provider_error_unmapped",
        extra={"error_code": CODE_ERROR},
    )
    return ImageProviderError(
        code=CODE_ERROR,
        message="Провайдер генерации изображений вернул неизвестную ошибку.",
        http_status=502,
        provider_body=str(exc)[:1000],
    )
