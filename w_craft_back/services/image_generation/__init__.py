"""Pluggable image-generation services.

Public entry points:
    - :class:`ImageProvider` — protocol for any provider implementation.
    - :class:`ImageProviderError` — uniform error raised to the view layer.
    - :func:`resolve_provider_for_user` — pick a provider based on user / override / env.
    - :data:`MODEL_REGISTRY` — registry of known model keys.
"""

from .base import ImageProvider
from .errors import ImageProviderError, map_to_provider_error
from .litellm_provider import LiteLLMProvider
from .registry import (
    MODEL_REGISTRY,
    ModelSpec,
    get_default_key,
    list_available_models,
    resolve_model,
)
from .resolver import resolve_provider_for_user

__all__ = [
    "ImageProvider",
    "ImageProviderError",
    "LiteLLMProvider",
    "MODEL_REGISTRY",
    "ModelSpec",
    "get_default_key",
    "list_available_models",
    "map_to_provider_error",
    "resolve_model",
    "resolve_provider_for_user",
]
