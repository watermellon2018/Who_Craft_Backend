"""Direct OpenRouter Images API provider and dynamic model discovery."""

from __future__ import annotations

import base64
import math
import os
import re
import threading
import time
from typing import Any, Mapping

import requests

from w_craft_back.storage_gateway import StorageGatewayError, normalize_image_bytes

from .errors import (
    CODE_BAD_RESPONSE,
    CODE_EDIT_NOT_SUPPORTED,
    CODE_FORBIDDEN,
    CODE_IMAGE_INPUT_NOT_SUPPORTED,
    CODE_NOT_CONFIGURED,
    CODE_UNAVAILABLE,
    ImageProviderError,
)
from .litellm_provider import _extract_image_api, _provider_output_count_limit
from .registry import OPENROUTER_IMAGES_KEY_PREFIX, ModelSpec

DEFAULT_OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CATALOG_TTL_SECONDS = 10 * 60
OPENROUTER_CATALOG_FAILURE_TTL_SECONDS = 30
_DEFAULT_TIMEOUT_SECONDS = 120.0
_CATALOG_TIMEOUT_SECONDS = 15.0

_FORWARD_PARAMETER_NAMES = (
    "resolution",
    "aspect_ratio",
    "size",
    "quality",
    "output_format",
    "background",
    "output_compression",
    "seed",
)
_EXPLICIT_SIZE_RE = re.compile(
    r"^(?P<width>[1-9][0-9]{1,4})x(?P<height>[1-9][0-9]{1,4})$"
)
_ASPECT_RATIOS = {
    "1:1", "1:2", "1:4", "1:8", "2:1", "2:3", "3:2", "3:4",
    "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "9:19.5",
    "19.5:9", "9:20", "20:9", "9:21", "21:9", "auto",
}

_catalog_lock = threading.Lock()
_catalog_condition = threading.Condition(_catalog_lock)
_catalog_specs: tuple[ModelSpec, ...] = ()
_catalog_fetched_at = 0.0
_catalog_failure: tuple[str, str, int, int | None] | None = None
_catalog_failure_at = 0.0
_catalog_refreshing = False


def _base_url() -> str:
    return (
        os.getenv("OPENROUTER_API_BASE_URL") or DEFAULT_OPENROUTER_API_BASE_URL
    ).strip().rstrip("/")


def _request_headers(*, require_key: bool) -> dict[str, str]:
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if require_key and not api_key:
        raise ImageProviderError(
            code=CODE_NOT_CONFIGURED,
            message=(
                "Модели OpenRouter Images требуют переменную окружения "
                "OPENROUTER_API_KEY."
            ),
            http_status=503,
        )
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    referer = (os.getenv("OPENROUTER_HTTP_REFERER") or "").strip()
    title = (os.getenv("OPENROUTER_APP_TITLE") or "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-OpenRouter-Title"] = title
    return headers


def _response_body(response: requests.Response) -> str | None:
    try:
        body = response.text
    except (AttributeError, OSError):
        return None
    return body if isinstance(body, str) else None


def _http_error(response: requests.Response) -> ImageProviderError:
    provider_status = int(getattr(response, "status_code", 0) or 0)
    common = {
        "provider_status": provider_status or None,
        "provider_body": _response_body(response),
    }
    if provider_status in {400, 422}:
        return ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="OpenRouter отклонил параметры генерации изображений.",
            http_status=400,
            **common,
        )
    if provider_status in {401, 403}:
        return ImageProviderError(
            code=CODE_FORBIDDEN,
            message="OpenRouter отклонил API-ключ или доступ к модели.",
            http_status=502,
            **common,
        )
    if provider_status == 402:
        return ImageProviderError(
            code=CODE_FORBIDDEN,
            message="OpenRouter отклонил запрос из-за состояния аккаунта провайдера.",
            http_status=502,
            **common,
        )
    if provider_status == 413:
        return ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Запрос к OpenRouter превышает допустимый размер.",
            http_status=413,
            **common,
        )
    if provider_status in {429, 502, 503, 529}:
        return ImageProviderError(
            code=CODE_UNAVAILABLE,
            message="OpenRouter Images временно недоступен. Попробуйте позже.",
            http_status=503,
            **common,
        )
    if provider_status in {504, 524}:
        return ImageProviderError(
            code=CODE_UNAVAILABLE,
            message="OpenRouter Images не ответил вовремя.",
            http_status=504,
            **common,
        )
    return ImageProviderError(
        code=CODE_UNAVAILABLE,
        message="OpenRouter Images вернул ошибку.",
        http_status=502,
        **common,
    )


def _network_error() -> ImageProviderError:
    return ImageProviderError(
        code=CODE_UNAVAILABLE,
        message="Не удалось соединиться с OpenRouter Images.",
        http_status=503,
    )


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Bound a provider-controlled JSON subtree before exposing/caching it."""

    if depth > 5:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100 or not isinstance(key, str):
                continue
            result[key[:100]] = _json_safe(item, depth=depth + 1)
        return result
    return None


def _normalize_descriptor(raw: Any) -> dict[str, Any] | None:
    if raw is False or raw is None:
        return None
    if raw is True:
        return {"type": "boolean"}
    if isinstance(raw, (list, tuple)):
        values = [item for item in _json_safe(raw) if isinstance(item, str)]
        return {"type": "enum", "values": values}
    if isinstance(raw, str):
        return {"type": raw[:50]}
    if not isinstance(raw, Mapping):
        return None

    safe = _json_safe(raw)
    if not isinstance(safe, dict):
        return None
    descriptor_type = safe.get("type") or safe.get("kind")
    if isinstance(descriptor_type, str):
        safe["type"] = descriptor_type[:50]
    for source in ("values", "enum", "options", "supported_values"):
        values = safe.get(source)
        if isinstance(values, list):
            safe["values"] = [value for value in values if isinstance(value, str)]
            safe.setdefault("type", "enum")
            break
    for canonical, aliases in {
        "min": ("min", "minimum", "min_items", "minItems"),
        "max": ("max", "maximum", "max_items", "maxItems"),
    }.items():
        for alias in aliases:
            value = safe.get(alias)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                safe[canonical] = value
                break
    return safe


def _normalize_supported_parameters(raw: Any) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        items = raw.items()
    elif isinstance(raw, list):
        collected: list[tuple[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                collected.append((item, True))
            elif isinstance(item, Mapping):
                name = item.get("name") or item.get("parameter")
                if isinstance(name, str):
                    descriptor = dict(item)
                    descriptor.pop("name", None)
                    descriptor.pop("parameter", None)
                    collected.append((name, descriptor))
        items = collected
    else:
        return parameters

    for index, (name, descriptor) in enumerate(items):
        if index >= 100 or not isinstance(name, str) or not name.strip():
            continue
        normalized = _normalize_descriptor(descriptor)
        if normalized is not None:
            parameters[name.strip()[:100]] = normalized
    return parameters


def _modalities(row: Mapping[str, Any], name: str) -> tuple[str, ...]:
    architecture = row.get("architecture")
    raw = row.get(name)
    if raw is None and isinstance(architecture, Mapping):
        raw = architecture.get(name)
    if not isinstance(raw, list):
        return ()
    return tuple(
        value.strip()[:50]
        for value in raw[:20]
        if isinstance(value, str) and value.strip()
    )


def _reference_capability(
    input_modalities: tuple[str, ...], supported_parameters: Mapping[str, Any]
) -> bool:
    descriptor = supported_parameters.get("input_references")
    if not isinstance(descriptor, Mapping):
        return False
    maximum = descriptor.get("max")
    return (
        "image" in input_modalities
        and isinstance(maximum, (int, float))
        and not isinstance(maximum, bool)
        and maximum >= 1
    )


def _supports_raster_output(supported_parameters: Mapping[str, Any]) -> bool:
    descriptor = supported_parameters.get("output_format")
    if not isinstance(descriptor, Mapping):
        return True
    values = descriptor.get("values")
    if not isinstance(values, list) or not values:
        return True
    return bool({"png", "jpeg", "webp"}.intersection(values))


def _parse_catalog(payload: Any) -> list[ModelSpec]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="OpenRouter вернул некорректный каталог моделей изображений.",
            http_status=502,
        )
    specs: list[ModelSpec] = []
    seen: set[str] = set()
    for row in payload["data"]:
        if not isinstance(row, Mapping):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()[:300]
        key = f"{OPENROUTER_IMAGES_KEY_PREFIX}{model_id}"
        if key in seen:
            continue
        input_modalities = _modalities(row, "input_modalities")
        output_modalities = _modalities(row, "output_modalities")
        if "image" not in output_modalities:
            continue
        parameters = _normalize_supported_parameters(row.get("supported_parameters"))
        supports_reference = _reference_capability(input_modalities, parameters)
        supports_generate = _supports_raster_output(parameters)
        name = row.get("name")
        description = row.get("description")
        specs.append(
            ModelSpec(
                key=key,
                label=(
                    name.strip()[:200]
                    if isinstance(name, str) and name.strip()
                    else model_id
                ),
                description=(
                    description.strip()[:4000]
                    if isinstance(description, str)
                    else ""
                ),
                backend="openrouter-images",
                model_id=model_id,
                mode="images",
                supports_generate=supports_generate,
                supports_edit=supports_generate and supports_reference,
                supports_reference=supports_generate and supports_reference,
                supported_parameters=parameters,
                input_modalities=input_modalities,
                output_modalities=output_modalities,
                requires_env=("OPENROUTER_API_KEY",),
            )
        )
        seen.add(key)
    if not specs:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="OpenRouter вернул пустой каталог моделей изображений.",
            http_status=502,
        )
    return specs


def _fetch_catalog(session: requests.Session | None = None) -> list[ModelSpec]:
    client = session or requests.Session()
    client.headers.update(_request_headers(require_key=False))
    try:
        response = client.get(
            f"{_base_url()}/images/models",
            timeout=_CATALOG_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise _network_error() from exc
    if response.status_code != 200:
        raise _http_error(response)
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="OpenRouter вернул некорректный JSON каталога моделей.",
            http_status=502,
            provider_status=response.status_code,
            provider_body=_response_body(response),
        ) from exc
    return _parse_catalog(payload)


def discover_openrouter_image_models(
    *,
    force_refresh: bool = False,
    session: requests.Session | None = None,
) -> list[ModelSpec]:
    """Return the 10-minute cached catalog, with stale-on-error fallback."""

    global _catalog_failure, _catalog_failure_at, _catalog_fetched_at
    global _catalog_refreshing, _catalog_specs
    now = time.monotonic()
    if (
        not force_refresh
        and _catalog_specs
        and now - _catalog_fetched_at < OPENROUTER_CATALOG_TTL_SECONDS
    ):
        return list(_catalog_specs)

    with _catalog_condition:
        now = time.monotonic()
        if (
            not force_refresh
            and _catalog_specs
            and now - _catalog_fetched_at < OPENROUTER_CATALOG_TTL_SECONDS
        ):
            return list(_catalog_specs)
        failure_is_recent = (
            _catalog_failure is not None
            and now - _catalog_failure_at
            < OPENROUTER_CATALOG_FAILURE_TTL_SECONDS
        )
        if not force_refresh and failure_is_recent:
            if _catalog_specs:
                return list(_catalog_specs)
            code, message, http_status, provider_status = _catalog_failure
            raise ImageProviderError(
                code=code,
                message=message,
                http_status=http_status,
                provider_status=provider_status,
            )
        if _catalog_refreshing:
            if _catalog_specs:
                return list(_catalog_specs)
            _catalog_condition.wait_for(
                lambda: not _catalog_refreshing,
                timeout=_CATALOG_TIMEOUT_SECONDS + 1,
            )
            if _catalog_specs:
                return list(_catalog_specs)
            if _catalog_failure is not None:
                code, message, http_status, provider_status = _catalog_failure
                raise ImageProviderError(
                    code=code,
                    message=message,
                    http_status=http_status,
                    provider_status=provider_status,
                )
            raise _network_error()
        _catalog_refreshing = True

    try:
        fresh_specs = _fetch_catalog(session=session)
    except ImageProviderError as exc:
        with _catalog_condition:
            _catalog_failure = (
                exc.code,
                exc.message,
                exc.http_status,
                exc.provider_status,
            )
            _catalog_failure_at = time.monotonic()
            _catalog_refreshing = False
            stale_specs = list(_catalog_specs)
            _catalog_condition.notify_all()
        if stale_specs:
            return stale_specs
        raise
    else:
        with _catalog_condition:
            _catalog_specs = tuple(fresh_specs)
            _catalog_fetched_at = time.monotonic()
            _catalog_failure = None
            _catalog_failure_at = 0.0
            _catalog_refreshing = False
            _catalog_condition.notify_all()
            return list(_catalog_specs)


def clear_openrouter_image_models_cache() -> None:
    """Clear process-local discovery state (primarily for tests/operations)."""

    global _catalog_failure, _catalog_failure_at, _catalog_fetched_at
    global _catalog_refreshing, _catalog_specs
    with _catalog_condition:
        _catalog_specs = ()
        _catalog_fetched_at = 0.0
        _catalog_failure = None
        _catalog_failure_at = 0.0
        _catalog_refreshing = False
        _catalog_condition.notify_all()


def _number_bound(descriptor: Any, name: str) -> float | None:
    if not isinstance(descriptor, Mapping):
        return None
    value = descriptor.get(name)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _validate_option(name: str, value: Any, descriptor: Any) -> Any:
    if name in {"output_compression", "seed"}:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message=f"Параметр '{name}' должен быть целым числом.",
                http_status=400,
            )
    elif not isinstance(value, str) or not value.strip():
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message=f"Параметр '{name}' должен быть непустой строкой.",
            http_status=400,
        )
    if name == "output_compression" and not 0 <= value <= 100:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Параметр 'output_compression' должен быть от 0 до 100.",
            http_status=400,
        )
    if name == "seed" and not -(2**63) <= value < 2**63:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Параметр 'seed' находится вне допустимого диапазона.",
            http_status=400,
        )
    if name == "resolution" and value not in {"512", "1K", "2K", "4K"}:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Параметр 'resolution' имеет недопустимое значение.",
            http_status=400,
        )
    if name == "aspect_ratio" and value not in _ASPECT_RATIOS:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Параметр 'aspect_ratio' имеет недопустимое значение.",
            http_status=400,
        )
    if name == "size":
        match = _EXPLICIT_SIZE_RE.match(value)
        if match is None and value not in {"512", "1K", "2K", "4K"}:
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="Параметр 'size' имеет недопустимый формат.",
                http_status=400,
            )
        if match and any(int(match.group(axis)) > 8192 for axis in ("width", "height")):
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="Размер изображения превышает допустимый предел.",
                http_status=400,
            )
    allowed_values = {
        "quality": {"auto", "low", "medium", "high"},
        "output_format": {"png", "jpeg", "webp"},
        "background": {"auto", "transparent", "opaque"},
    }
    if name in allowed_values and value not in allowed_values[name]:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message=f"Параметр '{name}' имеет недопустимое значение.",
            http_status=400,
        )

    if isinstance(descriptor, Mapping):
        values = descriptor.get("values")
        if isinstance(values, list) and values and value not in values:
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message=f"Параметр '{name}' не поддерживает выбранное значение.",
                http_status=400,
            )
        minimum = _number_bound(descriptor, "min")
        maximum = _number_bound(descriptor, "max")
        if isinstance(value, (int, float)) and (
            (minimum is not None and value < minimum)
            or (maximum is not None and value > maximum)
        ):
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message=f"Параметр '{name}' находится вне диапазона модели.",
                http_status=400,
            )
    return value.strip() if isinstance(value, str) else value


def _variant_count(spec: ModelSpec, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Количество вариантов должно быть целым числом.",
            http_status=400,
        )
    descriptor = spec.supported_parameters.get("n")
    descriptor_maximum = _number_bound(descriptor, "max")
    maximum = 1
    if descriptor is not None and descriptor_maximum is not None:
        maximum = min(10, _provider_output_count_limit(), int(descriptor_maximum))
    if not 1 <= value <= maximum:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message=f"Количество вариантов должно быть от 1 до {maximum}.",
            http_status=400,
        )
    return value


class OpenRouterImagesProvider:
    """Direct adapter for OpenRouter's dedicated buffered Images API."""

    def __init__(
        self,
        spec: ModelSpec,
        *,
        session: requests.Session | None = None,
    ) -> None:
        if spec.backend != "openrouter-images" or spec.mode != "images":
            raise ValueError("OpenRouterImagesProvider requires an images ModelSpec")
        self.spec = spec
        self.name = spec.key
        self.model_id = spec.model_id
        self.session = session or requests.Session()
        self.session.headers.update(_request_headers(require_key=True))
        self.session.headers.setdefault("Content-Type", "application/json")

    def supports_edit(self) -> bool:
        return self.spec.supports_edit

    def supports_reference(self) -> bool:
        return self.spec.supports_reference

    def _options(
        self,
        *,
        aspect_ratio: str | None,
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        supplied = dict(kwargs)
        # Some callers group provider settings for transport. We still unpack
        # only the fixed public whitelist and never pass through extra_body.
        grouped = supplied.get("provider_options")
        if isinstance(grouped, Mapping):
            for name in _FORWARD_PARAMETER_NAMES:
                if name not in supplied and name in grouped:
                    supplied[name] = grouped[name]
        if aspect_ratio is not None:
            grouped_aspect = supplied.get("aspect_ratio")
            if grouped_aspect is not None and grouped_aspect != aspect_ratio:
                raise ImageProviderError(
                    code=CODE_BAD_RESPONSE,
                    message=(
                        "Параметр 'aspect_ratio' задан с конфликтующими "
                        "значениями."
                    ),
                    http_status=400,
                )
            supplied["aspect_ratio"] = aspect_ratio

        options: dict[str, Any] = {}
        for name in _FORWARD_PARAMETER_NAMES:
            if name not in supplied or supplied[name] is None:
                continue
            descriptor = self.spec.supported_parameters.get(name)
            if descriptor is None:
                continue
            options[name] = _validate_option(name, supplied[name], descriptor)
        explicit_size = options.get("size")
        if (
            isinstance(explicit_size, str)
            and _EXPLICIT_SIZE_RE.match(explicit_size)
            and ("resolution" in options or "aspect_ratio" in options)
        ):
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message=(
                    "Явный размер WIDTHxHEIGHT нельзя сочетать с resolution "
                    "или aspect_ratio."
                ),
                http_status=400,
            )
        return options

    def _post(self, payload: dict[str, Any], *, timeout: Any) -> list[bytes]:
        request_timeout = _DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
        if (
            not isinstance(request_timeout, (int, float))
            or isinstance(request_timeout, bool)
            or not 0 < request_timeout <= 600
        ):
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="Таймаут генерации должен быть от 0 до 600 секунд.",
                http_status=400,
            )
        try:
            response = self.session.post(
                f"{_base_url()}/images",
                json=payload,
                timeout=float(request_timeout),
            )
        except requests.RequestException as exc:
            raise _network_error() from exc
        if response.status_code != 200:
            raise _http_error(response)
        try:
            response_payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="OpenRouter Images вернул некорректный JSON.",
                http_status=502,
                provider_status=response.status_code,
                provider_body=_response_body(response),
            ) from exc
        return _extract_image_api(response_payload)

    def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str | None = None,
        variant_count: int = 1,
        **kwargs: Any,
    ) -> list[bytes]:
        if not self.spec.supports_generate:
            raise ImageProviderError(
                code="IMAGE_PROVIDER_GENERATE_NOT_SUPPORTED",
                message=(
                    f"Модель '{self.spec.label}' не возвращает поддерживаемый "
                    "растровый формат изображения."
                ),
                http_status=400,
            )
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="Промпт генерации не может быть пустым.",
                http_status=400,
            )
        n = _variant_count(self.spec, variant_count)
        payload: dict[str, Any] = {
            "stream": False,
            "model": self.model_id,
            "prompt": prompt.strip(),
            "n": n,
        }
        payload.update(self._options(aspect_ratio=aspect_ratio, kwargs=kwargs))
        return self._post(payload, timeout=kwargs.get("timeout"))

    def generate_with_reference(
        self,
        prompt: str,
        image_bytes: bytes,
        *,
        mime_type: str = "image/png",
        variant_count: int = 1,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> list[bytes]:
        if not self.spec.supports_reference:
            raise ImageProviderError(
                code=CODE_IMAGE_INPUT_NOT_SUPPORTED,
                message=f"Модель '{self.spec.label}' не поддерживает референсы.",
                http_status=400,
            )
        try:
            normalized = normalize_image_bytes(image_bytes)
        except StorageGatewayError as exc:
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="Референс не является допустимым изображением.",
                http_status=400,
            ) from exc
        data_url = (
            f"data:{normalized.mime_type};base64,"
            f"{base64.b64encode(normalized.data).decode('ascii')}"
        )
        payload: dict[str, Any] = {
            "stream": False,
            "model": self.model_id,
            "prompt": prompt.strip() if isinstance(prompt, str) else prompt,
            "n": _variant_count(self.spec, variant_count),
            "input_references": [
                {"type": "image_url", "image_url": {"url": data_url}}
            ],
        }
        if not isinstance(payload["prompt"], str) or not payload["prompt"]:
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="Промпт генерации не может быть пустым.",
                http_status=400,
            )
        payload.update(
            self._options(
                aspect_ratio=kwargs.pop("aspect_ratio", None),
                kwargs=kwargs,
            )
        )
        return self._post(payload, timeout=timeout)

    def edit(
        self,
        image_bytes: bytes,
        instruction: str,
        *,
        mime_type: str = "image/png",
        **kwargs: Any,
    ) -> bytes:
        if not self.spec.supports_edit:
            raise ImageProviderError(
                code=CODE_EDIT_NOT_SUPPORTED,
                message=f"Модель '{self.spec.label}' не поддерживает редактирование.",
                http_status=400,
            )
        images = self.generate_with_reference(
            instruction,
            image_bytes,
            mime_type=mime_type,
            variant_count=1,
            **kwargs,
        )
        return images[0]
