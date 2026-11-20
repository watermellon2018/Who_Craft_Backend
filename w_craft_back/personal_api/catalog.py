"""Capabilities come from existing Craft registries, never from submitted keys."""

from typing import Any

from w_craft_back.services.image_generation.errors import ImageProviderError
from w_craft_back.services.image_generation.registry import (
    list_available_models,
    resolve_model,
)
from w_craft_back.services.text_generation.registry import TEXT_MODELS

from .errors import PersonalAPIError
from .models import MODALITIES, PROVIDERS


def image_provider(spec: Any) -> str:
    if spec.backend == 'gemini-native' or spec.model_id.startswith('gemini/'):
        return 'gemini'
    if spec.backend == 'openrouter-images' or spec.model_id.startswith('openrouter/'):
        return 'openrouter'
    return ''


def audio_provider(backend: str) -> str:
    return {
        'google-lyria': 'gemini', 'openrouter-lyria': 'openrouter',
        'stability': 'stability', 'elevenlabs-music-v2': 'elevenlabs',
        'minimax-music-3': 'minimax',
    }.get(backend, '')


def model_catalog(*, include_dynamic: bool = False) -> list[dict]:
    from w_craft_back.movie.music.providers.model_registry import audio_model_specs
    from w_craft_back.movie.sound_effects.providers.elevenlabs import MODEL_KEY
    from w_craft_back.services.video_generation.registry import list_models

    result = []
    for row in list_available_models(include_dynamic=include_dynamic):
        # Map public catalog metadata without requiring platform credentials.
        provider = (
            'gemini' if row['backend'] == 'gemini-native'
            or row['model_id'].startswith('gemini/') else 'openrouter'
            if row['backend'] == 'openrouter-images'
            or row['model_id'].startswith('openrouter/') else ''
        )
        if not provider:
            continue
        operations = [operation for operation, enabled in (
            ('generate', row['supports_generate']),
            ('edit', row['supports_edit'] and row['backend'] != 'gemini-native'),
            ('reference', row['supports_reference']),
        ) if enabled]
        result.append({
            'key': row['key'], 'label': row['label'], 'modality': 'image',
            'providers': [provider], 'operations': operations, 'isFree': False,
        })
    for model in TEXT_MODELS:
        for route in model.routes:
            provider = route.split('/', 1)[0]
            if provider in {'gemini', 'openrouter'}:
                result.append({
                    'key': route, 'label': model.label, 'modality': 'text',
                    'providers': [provider],
                    'operations': ['generate', 'structured'], 'isFree': False,
                })
    for model in audio_model_specs():
        providers = list(dict.fromkeys(
            audio_provider(route.backend_name) for route in model.routes
            if audio_provider(route.backend_name)
        ))
        if not providers:
            continue
        result.append({
            'key': model.key, 'label': model.display_name, 'modality': 'music',
            'providers': providers,
            'operations': ['generate'] + (
                ['reference'] if model.capabilities.supports_audio_reference else []
            ), 'isFree': False,
        })
    result.append({
        'key': MODEL_KEY, 'label': 'ElevenLabs Sound Effects v2',
        'modality': 'sound_effect', 'providers': ['elevenlabs'],
        'operations': ['generate'], 'isFree': False,
    })
    result.extend({
        'key': model.key, 'label': model.label, 'modality': 'video',
        'providers': [model.provider], 'operations': ['generate'], 'isFree': False,
    } for model in list_models(include_dynamic=include_dynamic))
    return result


def provider_catalog(models: list[dict]) -> list[dict]:
    return [{
        'id': provider, 'label': label,
        'modalities': [modality for modality in MODALITIES if any(
            model['modality'] == modality and provider in model['providers']
            for model in models
        )],
    } for provider, label in PROVIDERS]


def validate_route(
    modality: str, provider: str, model_key: str, operation: str = 'generate',
) -> None:
    if modality == 'image':
        try:
            spec = resolve_model(model_key)
        except ImageProviderError:
            raise PersonalAPIError(
                'PERSONAL_API_MODEL_UNAVAILABLE',
                'The selected image model is unavailable.',
            ) from None
        supported = {
            'generate': spec.supports_generate,
            'edit': spec.supports_edit and spec.backend != 'gemini-native',
            'reference': spec.supports_reference,
        }
        if image_provider(spec) == provider and supported.get(operation, False):
            return
    else:
        if any(
            row['modality'] == modality and row['key'] == model_key
            and provider in row['providers'] and operation in row['operations']
            for row in model_catalog()
        ):
            return
    raise PersonalAPIError(
        'PERSONAL_API_MODEL_INCOMPATIBLE',
        'This connection and model do not support the requested operation.',
    )
