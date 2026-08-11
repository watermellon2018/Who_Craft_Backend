"""Pluggable image-generation services.

Public entry points:
    - :class:`ImageProvider` — protocol for any provider implementation.
    - :class:`ImageProviderError` — uniform error raised to the view layer.
    - :func:`resolve_provider_for_user` — pick a provider for a user/request.
    - :data:`MODEL_REGISTRY` — registry of known model keys.
"""

from .base import ImageProvider
from .errors import ImageProviderError, map_to_provider_error
from .litellm_provider import LiteLLMProvider
from .openrouter_images import (
    OpenRouterImagesProvider,
    clear_openrouter_image_models_cache,
    discover_openrouter_image_models,
)
from .registry import (
    MODEL_REGISTRY,
    OPENROUTER_IMAGES_KEY_PREFIX,
    ModelSpec,
    deserialize_model_spec,
    get_default_key,
    is_configured,
    list_available_models,
    model_catalog_row,
    model_spec_from_snapshot,
    model_spec_to_snapshot,
    resolve_model,
    serialize_model_spec,
)
from .resolver import (
    provider_from_spec,
    resolve_current_for_user,
    resolve_provider_for_user,
)

__all__ = [
    "ImageProvider",
    "ImageProviderError",
    "LiteLLMProvider",
    "MODEL_REGISTRY",
    "ModelSpec",
    "OPENROUTER_IMAGES_KEY_PREFIX",
    "OpenRouterImagesProvider",
    "clear_openrouter_image_models_cache",
    "deserialize_model_spec",
    "discover_openrouter_image_models",
    "get_default_key",
    "is_configured",
    "list_available_models",
    "map_to_provider_error",
    "model_catalog_row",
    "model_spec_from_snapshot",
    "model_spec_to_snapshot",
    "provider_from_spec",
    "resolve_current_for_user",
    "resolve_model",
    "resolve_provider_for_user",
    "serialize_model_spec",
]
