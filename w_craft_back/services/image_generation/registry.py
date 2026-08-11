"""Static and dynamically discovered image-generation models."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .errors import CODE_EDIT_NOT_SUPPORTED, CODE_MODEL_UNKNOWN, ImageProviderError

logger = logging.getLogger(__name__)

OPENROUTER_IMAGES_KEY_PREFIX = "openrouter-images:"


@dataclass(frozen=True)
class ModelSpec:
    """Serializable description of one image model and its capabilities."""

    key: str
    label: str
    backend: str
    model_id: str
    mode: str
    supports_generate: bool
    supports_edit: bool
    supports_reference: bool = False
    description: str = ""
    supported_parameters: dict[str, Any] = field(default_factory=dict)
    input_modalities: tuple[str, ...] = field(default_factory=tuple)
    output_modalities: tuple[str, ...] = field(default_factory=tuple)
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
        supports_reference=False,
        supported_parameters={
            "aspect_ratio": {
                "type": "enum",
                "values": ["1:1", "3:4", "4:3", "16:9", "9:16"],
            },
            "n": {"type": "range", "min": 1, "max": 4},
        },
        input_modalities=("text",),
        output_modalities=("image",),
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
        supports_reference=True,
        supported_parameters={
            "input_references": {"type": "range", "min": 0, "max": 1},
            "n": {"type": "range", "min": 1, "max": 4},
        },
        input_modalities=("text", "image"),
        output_modalities=("image", "text"),
        requires_env=("GEMINI_API_KEY",),
        default=True,
    ),
    "openrouter-flash-image": ModelSpec(
        key="openrouter-flash-image",
        label="Gemini Flash Image via OpenRouter",
        backend="litellm",
        model_id="openrouter/google/gemini-3.1-flash-image-preview",
        mode="chat",
        supports_generate=True,
        supports_edit=True,
        supports_reference=True,
        supported_parameters={
            "input_references": {"type": "range", "min": 0, "max": 1},
            "n": {"type": "range", "min": 1, "max": 4},
        },
        input_modalities=("text", "image"),
        output_modalities=("image", "text"),
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
        supports_reference=False,
        supported_parameters={
            "aspect_ratio": {
                "type": "enum",
                "values": ["1:1", "3:4", "16:9", "square", "vertical", "horizontal"],
            },
            "n": {"type": "range", "min": 1, "max": 4},
        },
        input_modalities=("text", "image"),
        output_modalities=("image",),
        requires_env=("GEMINI_API_KEY",),
    ),
}


def _static_default_key() -> str:
    for spec in MODEL_REGISTRY.values():
        if spec.default:
            return spec.key
    return "gemini-flash-image"


def _dynamic_specs(*, force_refresh: bool = False) -> list[ModelSpec]:
    # Lazy import avoids a registry/provider import cycle and, importantly,
    # performs no network access while Django imports modules.
    from .openrouter_images import discover_openrouter_image_models

    return discover_openrouter_image_models(force_refresh=force_refresh)


def get_default_key() -> str:
    """Return the configured default when it is a known static/dynamic key."""

    env_default = (os.getenv("DEFAULT_IMAGE_MODEL") or "").strip()
    if env_default in MODEL_REGISTRY:
        return env_default
    if env_default.startswith(OPENROUTER_IMAGES_KEY_PREFIX):
        try:
            if any(spec.key == env_default for spec in _dynamic_specs()):
                return env_default
        except ImageProviderError:
            # Settings/catalog pages must remain usable during an upstream
            # outage. Actual provider resolution remains fail-closed.
            logger.warning("OpenRouter image default could not be validated")
    return _static_default_key()


def _find_dynamic_model(key: str) -> ModelSpec | None:
    return next((spec for spec in _dynamic_specs() if spec.key == key), None)


def resolve_model(key: str | None, *, require_edit: bool = False) -> ModelSpec:
    """Validate a static or catalog-backed model key."""

    effective = (key or "").strip() or get_default_key()
    spec = MODEL_REGISTRY.get(effective)
    if spec is None and effective.startswith(OPENROUTER_IMAGES_KEY_PREFIX):
        # Never turn an arbitrary slug into a billable provider request. A
        # dynamic key is valid only if it was returned by the catalog.
        spec = _find_dynamic_model(effective)
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
    """Return whether all environment variables required by ``spec`` are set."""

    return all(bool((os.getenv(var) or "").strip()) for var in spec.requires_env)


def model_catalog_row(spec: ModelSpec, *, default_key: str | None = None) -> dict:
    """Render the stable public model-catalog schema."""

    effective_default = default_key if default_key is not None else get_default_key()
    return {
        "key": spec.key,
        "label": spec.label,
        "description": spec.description,
        "backend": spec.backend,
        "model_id": spec.model_id,
        "mode": spec.mode,
        "supports_generate": spec.supports_generate,
        "supports_edit": spec.supports_edit,
        "supports_reference": spec.supports_reference,
        "supported_parameters": json.loads(
            json.dumps(spec.supported_parameters, ensure_ascii=False)
        ),
        "input_modalities": list(spec.input_modalities),
        "output_modalities": list(spec.output_modalities),
        "default": spec.key == effective_default,
        "configured": is_configured(spec),
        "requires_env": list(spec.requires_env),
    }


def list_available_models(*, include_dynamic: bool = True) -> list[dict]:
    """List static models plus OpenRouter's current image catalog.

    Discovery failures degrade to the static registry. The discovery layer
    itself serves its last-known-good catalog after cache expiry.
    """

    specs = list(MODEL_REGISTRY.values())
    if include_dynamic:
        try:
            dynamic = _dynamic_specs()
        except ImageProviderError:
            logger.warning("OpenRouter image catalog unavailable; using static models")
        else:
            known_keys = {spec.key for spec in specs}
            specs.extend(spec for spec in dynamic if spec.key not in known_keys)
    env_default = (os.getenv("DEFAULT_IMAGE_MODEL") or "").strip()
    available_keys = {spec.key for spec in specs}
    default_key = (
        env_default if env_default in available_keys else _static_default_key()
    )
    return [model_catalog_row(spec, default_key=default_key) for spec in specs]


def serialize_model_spec(spec: ModelSpec) -> dict[str, Any]:
    """Create a JSON-safe model snapshot suitable for a queued job JSONField."""

    payload = asdict(spec)
    payload["input_modalities"] = list(spec.input_modalities)
    payload["output_modalities"] = list(spec.output_modalities)
    payload["requires_env"] = list(spec.requires_env)
    # Round-tripping rejects accidental non-JSON values in future fields.
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"Model snapshot field '{field_name}' must be a string list")
    return tuple(value)


def deserialize_model_spec(snapshot: Mapping[str, Any] | str) -> ModelSpec:
    """Restore a trusted provider spec without consulting the live catalog."""

    if isinstance(snapshot, str):
        try:
            raw = json.loads(snapshot)
        except json.JSONDecodeError as exc:
            raise ValueError("Model snapshot is not valid JSON") from exc
    elif isinstance(snapshot, Mapping):
        raw = dict(snapshot)
    else:
        raise ValueError("Model snapshot must be a JSON object")
    if not isinstance(raw, dict):
        raise ValueError("Model snapshot must be a JSON object")

    required_strings = ("key", "label", "backend", "model_id", "mode")
    for name in required_strings:
        if not isinstance(raw.get(name), str) or not raw[name].strip():
            raise ValueError(f"Model snapshot field '{name}' must be a string")
    supported_parameters = raw.get("supported_parameters", {})
    if not isinstance(supported_parameters, dict):
        raise ValueError("Model snapshot supported_parameters must be an object")
    try:
        safe_parameters = json.loads(json.dumps(
            supported_parameters,
            ensure_ascii=False,
            allow_nan=False,
        ))
    except (TypeError, ValueError) as exc:
        raise ValueError("Model snapshot parameters are not JSON-safe") from exc

    boolean_fields = (
        "supports_generate",
        "supports_edit",
        "supports_reference",
        "default",
    )
    if any(
        name in raw and not isinstance(raw[name], bool) for name in boolean_fields
    ):
        raise ValueError("Model snapshot capability fields must be booleans")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ValueError("Model snapshot description must be a string")

    return ModelSpec(
        key=raw["key"],
        label=raw["label"],
        backend=raw["backend"],
        model_id=raw["model_id"],
        mode=raw["mode"],
        supports_generate=raw.get("supports_generate", False),
        supports_edit=raw.get("supports_edit", False),
        supports_reference=raw.get("supports_reference", False),
        description=description,
        supported_parameters=safe_parameters,
        input_modalities=_string_tuple(
            raw.get("input_modalities", []), "input_modalities"
        ),
        output_modalities=_string_tuple(
            raw.get("output_modalities", []), "output_modalities"
        ),
        requires_env=_string_tuple(raw.get("requires_env", []), "requires_env"),
        default=raw.get("default", False),
    )


# Explicit aliases read naturally at job enqueue/consume call sites.
model_spec_to_snapshot = serialize_model_spec
model_spec_from_snapshot = deserialize_model_spec
