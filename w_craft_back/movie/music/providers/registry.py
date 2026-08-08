"""Explicit registry for audio providers; no silent production fallback."""

from __future__ import annotations

from django.conf import settings

from .base import AudioProvider, MusicProviderError
from .mock import MockAudioProvider


_PROVIDER_FACTORIES = {"mock": MockAudioProvider}


def get_music_provider(provider_name: str | None = None) -> AudioProvider:
    """Resolve the configured provider or raise a stable safe error."""

    name = str(
        provider_name
        or getattr(settings, "MUSIC_GENERATION_PROVIDER", "mock")
        or "mock"
    ).strip().lower()
    factory = _PROVIDER_FACTORIES.get(name)
    if factory is None:
        raise MusicProviderError(
            "The selected music provider is not configured.",
            code="MUSIC_PROVIDER_NOT_CONFIGURED",
            http_status=503,
            retryable=True,
        )
    return factory()


def get_music_provider_capabilities(
    provider_name: str | None = None,
) -> dict[str, object]:
    """Return the stable public capability object for the effective provider."""

    return get_music_provider(provider_name).capabilities().as_public_dict()
