"""Documented Gemini models and a validated, cached OpenRouter video catalog."""

from dataclasses import asdict
from decimal import Decimal
import math
import re

from django.conf import settings
from django.core.cache import cache
from django.views.decorators.debug import sensitive_variables

from .contracts import VideoModelSpec, VideoProviderError


_CACHE_KEY = 'craft.video-models.openrouter.v1'
_REFRESH_KEY = _CACHE_KEY + '.refreshed'
_MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,180}\Z')
_ASPECT_RATIOS = {'16:9', '9:16', '1:1', '4:3', '3:4', '3:2', '2:3', '21:9', '9:21'}
_RESOLUTIONS = {'360p', '480p', '720p', '768p', '1080p', '1K', '2K', '4K', '4k'}


def _gemini(model: str, label: str, *, lite: bool = False) -> VideoModelSpec:
    return VideoModelSpec(
        key=f'gemini-video:{model}', provider='gemini', provider_model=model,
        label=label, durations=(4, 6, 8), aspect_ratios=('16:9', '9:16'),
        resolutions=('720p', '1080p') if lite else ('720p', '1080p', '4k'),
        supports_start_frame=True, supports_end_frame=True,
        supports_references=not lite, max_references=0 if lite else 3,
        supports_audio=True, audio_always_on=True, high_resolution_duration=8,
    )


STATIC_MODELS = (
    _gemini('veo-3.1-generate-preview', 'Veo 3.1 · Gemini'),
    _gemini('veo-3.1-fast-generate-preview', 'Veo 3.1 Fast · Gemini'),
    _gemini('veo-3.1-lite-generate-preview', 'Veo 3.1 Lite · Gemini', lite=True),
    # A conservative subset documented by the OpenRouter API examples. A live
    # valid catalog replaces this entry, including its supported durations.
    VideoModelSpec(
        key='openrouter-video:google/veo-3.1', provider='openrouter',
        provider_model='google/veo-3.1', label='Veo 3.1 · OpenRouter',
        durations=(8,), aspect_ratios=('16:9',), resolutions=('720p',),
        supports_start_frame=True, supports_end_frame=True, supports_audio=True,
    ),
)


def parse_openrouter_models(payload: object) -> tuple[VideoModelSpec, ...]:
    """Unknown or malformed capabilities never become a permissive default."""
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise VideoProviderError('VIDEO_CATALOG_INVALID')
    result = {}
    for row in payload['data'][:500]:
        if not isinstance(row, dict):
            continue
        model_id = row.get('id')
        duration_values = row.get('supported_durations')
        ratios = row.get('supported_aspect_ratios')
        resolutions = row.get('supported_resolutions')
        if (
            not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id)
            or '..' in model_id or not isinstance(duration_values, list)
            or not isinstance(ratios, list) or not isinstance(resolutions, list)
        ):
            continue
        durations = tuple(sorted({
            value for value in duration_values
            if type(value) is int and 1 <= value <= 120
        }))
        ratios = tuple(dict.fromkeys(
            value for value in ratios
            if isinstance(value, str) and value in _ASPECT_RATIOS
        ))
        resolutions = tuple(dict.fromkeys(
            value for value in resolutions
            if isinstance(value, str) and value in _RESOLUTIONS
        ))
        if not durations or not ratios or not resolutions:
            continue
        frames = row.get('supported_frame_images')
        frames = frames if isinstance(frames, list) else []
        name = row.get('name')
        label = name[:150] if isinstance(name, str) and name.strip() else model_id
        key = f'openrouter-video:{model_id}'
        result[key] = VideoModelSpec(
            key=key, provider='openrouter', provider_model=model_id,
            label=f'{label} · OpenRouter', durations=durations,
            aspect_ratios=ratios, resolutions=resolutions,
            supports_start_frame='first_frame' in frames,
            supports_end_frame='last_frame' in frames and 'first_frame' in frames,
            supports_audio=row.get('generate_audio') is True,
        )
    if not result:
        raise VideoProviderError('VIDEO_CATALOG_INVALID')
    return tuple(result.values())


@sensitive_variables('api_key')
def list_models(
    *, include_dynamic: bool = False, api_key: str | None = None,
) -> tuple[VideoModelSpec, ...]:
    """Cache public metadata only; an explicit personal key stays per request."""
    if include_dynamic and not cache.get(_REFRESH_KEY):
        try:
            refresh_openrouter_models(api_key=api_key)
        except VideoProviderError:
            # Throttle outage retries; retain the prior valid public catalog.
            cache.set(_REFRESH_KEY, True, 60)
    dynamic = cache.get(_CACHE_KEY) or ()
    dynamic = tuple(model for model in dynamic if isinstance(model, VideoModelSpec))
    models = {
        model.key: model for model in STATIC_MODELS
        if not dynamic or model.provider != 'openrouter'
    }
    models.update((model.key, model) for model in dynamic)
    return tuple(models.values())


def has_dynamic_catalog() -> bool:
    return bool(cache.get(_CACHE_KEY))


@sensitive_variables('api_key', 'adapter')
def refresh_openrouter_models(
    *, api_key: str | None = None,
) -> tuple[VideoModelSpec, ...]:
    """Explicit refresh reports failures and never replaces a valid catalog with one."""
    from .adapters import create_adapter

    source = 'personal' if api_key is not None else 'platform'
    adapter = create_adapter('openrouter', api_key=api_key, source=source)
    models = parse_openrouter_models(adapter.catalog())
    cache.set(_CACHE_KEY, models, 86400)
    cache.set(_REFRESH_KEY, True, 300)
    return models


def resolve_model(key: str) -> VideoModelSpec:
    for model in list_models():
        if model.key == key:
            return model
    raise VideoProviderError('VIDEO_MODEL_UNAVAILABLE')


def is_configured(model: VideoModelSpec) -> bool:
    return bool(getattr(settings, {
        'gemini': 'GEMINI_API_KEY', 'openrouter': 'OPENROUTER_API_KEY',
    }.get(model.provider, ''), ''))


def compatible_model(
    model: VideoModelSpec, *, aspect_ratio: str, resolution: str,
    has_start_frame: bool, has_end_frame: bool, has_references: bool = False,
) -> bool:
    return bool(
        aspect_ratio in model.aspect_ratios and resolution in model.resolutions
        and (not has_start_frame or model.supports_start_frame)
        and (not has_end_frame or (has_start_frame and model.supports_end_frame))
        and (not has_references or model.supports_references)
        and (not has_references or not has_start_frame
             or model.supports_references_with_frames)
    )


def nearest_duration(
    model: VideoModelSpec, seconds: float, *, resolution: str = '720p',
    has_end_frame: bool = False, has_references: bool = False,
) -> int:
    """Prefer the longer duration on ties; never edit the source storyboard."""
    if (
        isinstance(seconds, bool) or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds) or seconds <= 0
        or resolution not in model.resolutions or not model.durations
        or (has_end_frame and not model.supports_end_frame)
    ):
        raise VideoProviderError('VIDEO_PARAMETERS_INCOMPATIBLE')
    durations = model.durations
    if model.high_resolution_duration and (
        resolution != '720p' or has_references
    ):
        durations = tuple(
            value for value in durations if value == model.high_resolution_duration
        )
    if not durations:
        raise VideoProviderError('VIDEO_PARAMETERS_INCOMPATIBLE')
    return min(durations, key=lambda value: (abs(value - seconds), -value))


def model_snapshot(model: VideoModelSpec) -> dict:
    """Pinned capabilities travel with a job across processes/catalog refreshes."""
    return asdict(model)


def model_from_snapshot(value: dict) -> VideoModelSpec:
    """Only consume server-owned snapshots; shape is checked defensively."""
    try:
        model = VideoModelSpec(**{
            **value, 'durations': tuple(value['durations']),
            'aspect_ratios': tuple(value['aspect_ratios']),
            'resolutions': tuple(value['resolutions']),
        })
        if (
            model.provider not in {'gemini', 'openrouter'}
            or not _MODEL_ID.fullmatch(model.provider_model)
            or '..' in model.provider_model
            or not model.durations
            or any(type(x) is not int or not 1 <= x <= 120 for x in model.durations)
            or not set(model.aspect_ratios) <= _ASPECT_RATIOS
            or not set(model.resolutions) <= _RESOLUTIONS
        ):
            raise ValueError
        return model
    except (KeyError, TypeError, ValueError):
        raise VideoProviderError('VIDEO_MODEL_UNAVAILABLE') from None


def estimate_cost(
    model: VideoModelSpec, duration: int, resolution: str = '720p',
) -> Decimal | None:
    # OpenRouter pricing_skus do not declare a stable unit in the public schema;
    # do not convert ambiguous rates into a billable Craft estimate.
    return None
