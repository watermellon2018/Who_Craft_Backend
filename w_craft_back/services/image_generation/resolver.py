"""Picks the right :class:`ImageProvider` for a request.

Priority chain:
    1. ``override`` — explicit ``image_model`` body param on the request.
    2. ``user.profile.image_generation_model`` — saved user preference.
    3. ``DEFAULT_IMAGE_MODEL`` env var → registry default.

Missing env keys never silently fall through to a different model — we
raise :class:`ImageProviderError` (``IMAGE_PROVIDER_NOT_CONFIGURED``) so
nobody accidentally bills the wrong account.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .base import ImageProvider
from .errors import CODE_NOT_CONFIGURED, ImageProviderError
from .gemini_native import GeminiNativeProvider
from .litellm_provider import LiteLLMProvider
from .openrouter_images import OpenRouterImagesProvider
from .registry import (
    MODEL_REGISTRY,
    ModelSpec,
    get_default_key,
    is_configured,
    resolve_model,
)

logger = logging.getLogger(__name__)


def _user_pref(user: Any) -> str | None:
    """Read ``user.profile.image_generation_model`` if present."""
    if user is None or not getattr(user, "is_authenticated", True):
        return None
    profile = getattr(user, "profile", None)
    if profile is None:
        # Avoid creating a profile here — that's the caller's job.
        return None
    value = getattr(profile, "image_generation_model", "") or ""
    return value.strip() or None


def _check_required_env(spec: ModelSpec) -> None:
    missing = [
        var for var in spec.requires_env if not (os.getenv(var) or "").strip()
    ]
    if missing:
        raise ImageProviderError(
            code=CODE_NOT_CONFIGURED,
            message=(
                f"Модель '{spec.label}' требует переменные окружения: "
                f"{', '.join(missing)}. Задайте их или выберите другую модель."
            ),
            http_status=503,
        )


def _resolve_key(user: Any, override: str | None) -> tuple[str, str]:
    """Return ``(key, source)`` where source is one of ``override|user|env|default``."""
    if override:
        return override.strip(), "override"
    pref = _user_pref(user)
    if pref:
        return pref, "user"
    env_default = (os.getenv("DEFAULT_IMAGE_MODEL") or "").strip()
    if env_default:
        return env_default, "env"
    return get_default_key(), "default"


def resolve_provider_for_user(
    user: Any,
    *,
    override: str | None = None,
    require_edit: bool = False,
) -> ImageProvider:
    """Pick a provider for ``user``.

    ``user`` may be ``None`` (anonymous request) — we just skip the profile
    lookup. ``override`` and the user preference are both validated against
    the registry; an unknown key raises ``IMAGE_MODEL_UNKNOWN``.
    """
    key, source = _resolve_key(user, override)
    logger.info("Image provider resolved: key=%s source=%s require_edit=%s",
                key, source, require_edit)
    spec = resolve_model(key, require_edit=require_edit)
    return provider_from_spec(spec)


def provider_from_spec(spec: ModelSpec) -> ImageProvider:
    """Construct a provider from a persisted spec without catalog discovery."""

    _check_required_env(spec)
    if spec.backend == "gemini-native":
        return GeminiNativeProvider(spec)
    if spec.backend == "litellm":
        return LiteLLMProvider(spec)
    if spec.backend == "openrouter-images":
        return OpenRouterImagesProvider(spec)
    raise ImageProviderError(
        code="IMAGE_PROVIDER_INVALID_BACKEND",
        message="Сохранённая модель использует неподдерживаемый backend.",
        http_status=500,
    )


def resolve_current_for_user(user: Any) -> dict:
    """Read the user's effective model + source (no provider instantiation).

    Used by the GET endpoint to render the settings page even when API keys
    are missing.
    """
    key, source = _resolve_key(user, None)
    try:
        spec = resolve_model(key)
    except ImageProviderError:
        # Stored key no longer in registry — fall back to the env/default key
        # without raising; FE can show a notice.
        env_default = (os.getenv("DEFAULT_IMAGE_MODEL") or "").strip()
        if env_default in MODEL_REGISTRY:
            fallback_key, fallback_source = env_default, "env"
        else:
            fallback_key = next(
                (item.key for item in MODEL_REGISTRY.values() if item.default),
                "gemini-flash-image",
            )
            fallback_source = "default"
        spec = resolve_model(fallback_key)
        key, source = fallback_key, fallback_source
    return {
        "key": key,
        "source": source,
        "configured": is_configured(spec),
    }
