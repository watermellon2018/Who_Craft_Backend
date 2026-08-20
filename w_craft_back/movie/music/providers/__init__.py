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
from .model_registry import (
    AudioModelSpec,
    AudioRouteSpec,
    ResolvedAudioModel,
    audio_model_specs,
    capabilities_from_snapshot,
    default_audio_model_key,
    pricing_from_snapshot,
    public_audio_model_catalog,
    resolve_audio_model,
    resolve_legacy_audio_route,
    resolved_from_snapshot,
)

__all__ = [
    "AudioProvider",
    "AudioProviderCapabilities",
    "AudioProviderPricing",
    "GeneratedAudio",
    "MusicProviderError",
    "ProviderSubmission",
    "get_music_provider",
    "get_music_provider_capabilities",
    "AudioModelSpec",
    "AudioRouteSpec",
    "ResolvedAudioModel",
    "audio_model_specs",
    "capabilities_from_snapshot",
    "default_audio_model_key",
    "pricing_from_snapshot",
    "public_audio_model_catalog",
    "resolve_audio_model",
    "resolve_legacy_audio_route",
    "resolved_from_snapshot",
]
