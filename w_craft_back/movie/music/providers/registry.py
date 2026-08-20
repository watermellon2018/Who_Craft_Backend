"""Explicit registry for audio providers; no silent production fallback."""

from __future__ import annotations

from django.conf import settings

from .base import AudioProvider, MusicProviderError
from .mock import MockAudioProvider
from .stability import StabilityAudioProvider


def _google_lyria_provider(*, model_name: str) -> AudioProvider:
    from .google_lyria import GoogleLyriaProvider

    return GoogleLyriaProvider(model_name=model_name)


def _openrouter_lyria_provider(*, model_name: str) -> AudioProvider:
    from .openrouter_lyria import OpenRouterLyriaProvider

    return OpenRouterLyriaProvider(model_name=model_name)


def _elevenlabs_music_provider(*, model_name: str) -> AudioProvider:
    from .elevenlabs_music import ElevenLabsMusicProvider

    return ElevenLabsMusicProvider(model_name=model_name)


def _minimax_music_provider(*, model_name: str) -> AudioProvider:
    from .minimax_music import MiniMaxMusicProvider

    return MiniMaxMusicProvider(model_name=model_name)


def get_music_provider(
    provider_name: str | None = None,
    *,
    model_name: str = "",
) -> AudioProvider:
    """Resolve the configured provider or raise a stable safe error."""

    name = str(
        provider_name
        or getattr(settings, "MUSIC_GENERATION_PROVIDER", "mock")
        or "mock"
    ).strip().lower()
    if name == "mock":
        return MockAudioProvider()
    if name == "stability":
        return StabilityAudioProvider(model_name=model_name or None)
    if name == "google-lyria":
        return _google_lyria_provider(model_name=model_name)
    if name == "openrouter-lyria":
        return _openrouter_lyria_provider(model_name=model_name)
    if name == "elevenlabs-music-v2":
        return _elevenlabs_music_provider(model_name=model_name)
    if name == "minimax-music-3":
        return _minimax_music_provider(model_name=model_name)
    if name not in {
        "mock",
        "stability",
        "google-lyria",
        "openrouter-lyria",
        "elevenlabs-music-v2",
        "minimax-music-3",
    }:
        raise MusicProviderError(
            "The selected music provider is not configured.",
            code="MUSIC_PROVIDER_NOT_CONFIGURED",
            http_status=503,
            retryable=True,
        )
    raise AssertionError("Unreachable provider registry branch.")


def get_music_provider_capabilities(
    provider_name: str | None = None,
) -> dict[str, object]:
    """Return the stable public capability object for the effective provider."""

    return get_music_provider(provider_name).capabilities().as_public_dict()
