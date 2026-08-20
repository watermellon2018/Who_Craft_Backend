"""Audio provider protocol, registry, and deterministic local mock."""

from .base import (
    AudioProvider,
    AudioProviderCapabilities,
    AudioProviderPricing,
    GeneratedAudio,
    MusicProviderError,
    ProviderSubmission,
)
from .registry import get_music_provider, get_music_provider_capabilities

__all__ = [
    "AudioProvider",
    "AudioProviderCapabilities",
    "AudioProviderPricing",
    "GeneratedAudio",
    "MusicProviderError",
    "ProviderSubmission",
    "get_music_provider",
    "get_music_provider_capabilities",
]
