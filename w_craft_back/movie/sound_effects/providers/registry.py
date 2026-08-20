"""Explicit single-provider registry; production never silently falls back."""

from w_craft_back.movie.sound_effects.errors import SoundEffectProviderError

from .elevenlabs import ElevenLabsSoundEffectsProvider


def get_sound_effect_provider(
    provider_name: str = "elevenlabs-sfx",
) -> ElevenLabsSoundEffectsProvider:
    if str(provider_name or "").strip().lower() != "elevenlabs-sfx":
        raise SoundEffectProviderError(
            "The selected sound-effects provider is unsupported.",
            code="SOUND_EFFECT_PROVIDER_NOT_CONFIGURED",
            retryable=False,
        )
    return ElevenLabsSoundEffectsProvider()
