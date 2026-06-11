"""Registry of supported image-generation models.

A user picks a model by ``key`` (stored on ``UserProfile.image_generation_model``).
The resolver translates the key into a concrete provider instance using the
:class:`ModelSpec` recorded here.

Adding a new model: insert a new entry into :data:`MODEL_REGISTRY` and make sure
the required env vars are documented in ``.env.example``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .errors import CODE_EDIT_NOT_SUPPORTED, CODE_MODEL_UNKNOWN, ImageProviderError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    backend: str               # "litellm" | "gemini-native"
    model_id: str              # what we pass to litellm or the native client
    mode: str                  # "image" (dedicated image API) | "chat" (chat-completions)
    supports_generate: bool
    supports_edit: bool
    requires_env: tuple[str, ...] = field(default_factory=tuple)
    default: bool = False


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "gemini-imagen-4": ModelSpec(
        key="gemini-imagen-4",
        label="Google Imagen 4",
        backend="litellm",
        model_id="gemini/imagen-4.0-generate-001",
        mode="image",
        supports_generate=True,
        supports_edit=False,
        requires_env=("GEMINI_API_KEY",),
    ),
    "gemini-flash-image": ModelSpec(
        key="gemini-flash-image",
        label="Gemini 2.5 Flash Image (Nano Banana)",
        backend="litellm",
        model_id="gemini/gemini-2.5-flash-image",
        mode="chat",
        supports_generate=True,
        supports_edit=True,
        requires_env=("GEMINI_API_KEY",),
        default=True,
    ),
    "openrouter-flash-image": ModelSpec(
        key="openrouter-flash-image",
        label="Gemini Flash Image via OpenRouter",
        backend="litellm",
        # OpenRouter moved Nano Banana to 3.1; the old 2.5 slug now returns 404.
        model_id="openrouter/google/gemini-3.1-flash-image-preview",
        mode="chat",
        supports_generate=True,
        supports_edit=True,
        requires_env=("OPENROUTER_API_KEY",),
    ),
    "gemini-native": ModelSpec(
        key="gemini-native",
        label="Gemini (native REST, legacy fallback)",
        backend="gemini-native",
        model_id="imagen-4.0-generate-001",
        mode="image",
        supports_generate=True,
        supports_edit=True,
        requires_env=("GEMINI_API_KEY",),
    ),
}


def get_default_key() -> str:
    """The default registry key when the user hasn't picked anything.

    Priority: ``DEFAULT_IMAGE_MODEL`` env var > the ``default=True`` entry in
    the registry > a hard-coded ``"gemini-flash-image"`` fallback.
    """
    env_default = (os.getenv("DEFAULT_IMAGE_MODEL") or "").strip()
    if env_default and env_default in MODEL_REGISTRY:
        return env_default
    for spec in MODEL_REGISTRY.values():
        if spec.default:
            return spec.key
    return "gemini-flash-image"


def resolve_model(key: str | None, *, require_edit: bool = False) -> ModelSpec:
    """Validate the given key and return its :class:`ModelSpec`.

    Raises :class:`ImageProviderError` with ``IMAGE_MODEL_UNKNOWN`` for unknown
    keys, and ``IMAGE_PROVIDER_EDIT_NOT_SUPPORTED`` when ``require_edit`` is
    set but the model can't edit.
    """
    effective = (key or "").strip() or get_default_key()
    spec = MODEL_REGISTRY.get(effective)
    if spec is None:
        raise ImageProviderError(
            code=CODE_MODEL_UNKNOWN,
            message=f"Неизвестная модель генерации изображений: '{effective}'.",
            http_status=400,
        )
    if require_edit and not spec.supports_edit:
        raise ImageProviderError(
            code=CODE_EDIT_NOT_SUPPORTED,
            message=(
                f"Модель '{spec.label}' не поддерживает редактирование изображений. "
                "Выберите другую модель в настройках профиля."
            ),
            http_status=400,
        )
    return spec


def is_configured(spec: ModelSpec) -> bool:
    """``True`` when every env var the model needs is set."""
    return all(bool(os.getenv(var)) for var in spec.requires_env)


def list_available_models() -> list[dict]:
    """Public list for the GET endpoint — safe to send to the FE."""
    default_key = get_default_key()
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "supports_generate": spec.supports_generate,
            "supports_edit": spec.supports_edit,
            "default": spec.key == default_key,
            "configured": is_configured(spec),
            "requires_env": list(spec.requires_env),
        }
        for spec in MODEL_REGISTRY.values()
    ]
