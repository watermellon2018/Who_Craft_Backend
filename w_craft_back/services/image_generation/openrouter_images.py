"""Direct OpenRouter Images API provider and dynamic model discovery."""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Mapping
from urllib.parse import quote

import requests

from w_craft_back.storage_gateway import StorageGatewayError, normalize_image_bytes

from .errors import (
    CODE_BAD_RESPONSE,
    CODE_BLOCKED,
    CODE_EDIT_NOT_SUPPORTED,
    CODE_FORBIDDEN,
    CODE_IMAGE_INPUT_NOT_SUPPORTED,
    CODE_NOT_CONFIGURED,
    CODE_UNAVAILABLE,
    ImageProviderError,
)
from .litellm_provider import _extract_image_api, _provider_output_count_limit
from .registry import OPENROUTER_IMAGES_KEY_PREFIX, ModelSpec
from .usage import merge_usage, normalized_response_usage

logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CATALOG_TTL_SECONDS = 10 * 60
OPENROUTER_CATALOG_FAILURE_TTL_SECONDS = 30
_DEFAULT_TIMEOUT_SECONDS = 120.0
_CATALOG_TIMEOUT_SECONDS = 15.0
_MODEL_ENDPOINT_TIMEOUT_SECONDS = 5.0
_CATALOG_PRICING_BUDGET_SECONDS = 15.0
_CATALOG_REFRESH_WAIT_SECONDS = (
    _CATALOG_TIMEOUT_SECONDS + _CATALOG_PRICING_BUDGET_SECONDS + 1.0
)
_TRANSIENT_RESPONSE_STATUSES = frozenset({429, 502, 503, 529})
_TRANSIENT_RESPONSE_DELAYS_SECONDS = (0.5, 1.0)

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

_ERROR_SCAN_FIELDS = frozenset({
    "blocked_reason",
    "code",
    "details",
    "error",
    "error_type",
    "finish_reason",
    "message",
    "metadata",
    "provider_code",
    "provider_error",
    "raw",
    "reason",
    "reasons",
    "type",
})
_ERROR_SCAN_MAX_DEPTH = 5
_ERROR_SCAN_MAX_ITEMS = 10
_ERROR_SCAN_MAX_LENGTH = 4096
_BLOCKED_ERROR_MARKERS = frozenset({
    "content_blocked",
    "content_filter",
    "content_filtered",
    "content_policy",
    "content_policy_violation",
    "image_safety",
    "prohibited_content",
    "refusal",
    "safety_block",
    "safety_blocked",
})
_BLOCKED_MESSAGE_PATTERNS = (
    re.compile(r"\bcontent\s+policy\s+violation\b", re.IGNORECASE),
    re.compile(r"\bprohibited\s+content\b", re.IGNORECASE),
    re.compile(
        r"\bfailed\s+due\s+to\s+(?:content\s+policy|safety)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:content|image|prompt|request)\b.{0,80}"
        r"\b(?:blocked|filtered|refused|rejected)\b.{0,80}"
        r"\b(?:moderation|policy|safety)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:blocked|filtered|refused|rejected)\b.{0,80}"
        r"\b(?:content\s+policy|moderation|safety)\b",
        re.IGNORECASE,
    ),
)

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


def _provider_error_tokens(
    response: requests.Response,
) -> tuple[str | None, str | None]:
    """Extract bounded machine-readable diagnostics without logging raw bodies."""

    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError):
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None, None
    metadata = error.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}

    def safe_token(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
            return None
        return value

    def marker_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")

    def limited_values(value: Any, *, depth: int = 0) -> Iterator[str]:
        if depth > _ERROR_SCAN_MAX_DEPTH:
            return
        if isinstance(value, str):
            bounded = value[:_ERROR_SCAN_MAX_LENGTH]
            yield bounded
            if (
                depth == _ERROR_SCAN_MAX_DEPTH
                or len(value) > _ERROR_SCAN_MAX_LENGTH
            ):
                return
            try:
                decoded = json.loads(bounded)
            except (TypeError, ValueError):
                return
            if not isinstance(decoded, str):
                yield from limited_values(decoded, depth=depth + 1)
            return
        if isinstance(value, Mapping):
            for field in _ERROR_SCAN_FIELDS:
                if field in value:
                    yield from limited_values(value[field], depth=depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value[:_ERROR_SCAN_MAX_ITEMS]:
                yield from limited_values(item, depth=depth + 1)

    candidates = tuple(
        limited_values(
            {
                "code": error.get("code"),
                "message": error.get("message"),
                "type": error.get("type"),
                "details": error.get("details"),
                "metadata": {
                    field: metadata.get(field)
                    for field in _ERROR_SCAN_FIELDS
                    if field in metadata
                },
            }
        )
    )
    blocked = any(
        marker_key(candidate) in _BLOCKED_ERROR_MARKERS
        or any(pattern.search(candidate) for pattern in _BLOCKED_MESSAGE_PATTERNS)
        for candidate in candidates
    )

    error_type = safe_token(metadata.get("error_type")) or safe_token(
        error.get("type")
    )
    provider_code = safe_token(metadata.get("provider_code")) or safe_token(
        error.get("code")
    )
    if blocked:
        error_type = error_type or "content_policy"
        if provider_code is None:
            provider_code = next(
                (
                    token
                    for candidate in candidates
                    if (token := safe_token(candidate)) is not None
                    and marker_key(token) in _BLOCKED_ERROR_MARKERS
                ),
                None,
            )
    return error_type, provider_code


def _http_error(response: requests.Response) -> ImageProviderError:
    provider_status = int(getattr(response, "status_code", 0) or 0)
    error_type, provider_code = _provider_error_tokens(response)
    common = {
        "provider_status": provider_status or None,
        "provider_body": _response_body(response),
    }
    if any(
        isinstance(token, str)
        and re.sub(r"[^a-z0-9]+", "_", token.casefold()).strip("_")
        in _BLOCKED_ERROR_MARKERS
        for token in (error_type, provider_code)
    ):
        return ImageProviderError(
            code=CODE_BLOCKED,
            message="Провайдер изображений отклонил запрос по правилам безопасности.",
            http_status=400,
            **common,
        )
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
        raw_pricing = _json_safe(row.get("pricing"))
        if isinstance(raw_pricing, Mapping):
            provider_pricing = dict(raw_pricing)
        elif isinstance(raw_pricing, list):
            provider_pricing = {"catalog": raw_pricing}
        else:
            provider_pricing = {}
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
                provider_pricing=provider_pricing,
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


def _endpoint_pricing_catalog(payload: Any) -> list[dict[str, str]]:
    """Normalize definitive per-endpoint pricing into a bounded safe catalog."""

    if not isinstance(payload, Mapping):
        return []
    endpoints = payload.get("endpoints")
    if not isinstance(endpoints, list):
        return []
    result: list[dict[str, str]] = []
    for endpoint in endpoints[:50]:
        if not isinstance(endpoint, Mapping):
            continue
        provider = endpoint.get("provider_slug") or endpoint.get("provider_name")
        pricing = endpoint.get("pricing")
        if not isinstance(pricing, list):
            continue
        for row in pricing[:100]:
            if not isinstance(row, Mapping):
                continue
            billable = row.get("billable")
            unit = row.get("unit")
            if not isinstance(billable, str) or not billable.strip():
                continue
            if not isinstance(unit, str) or not unit.strip():
                continue
            try:
                cost = Decimal(str(row.get("cost_usd")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not cost.is_finite() or cost < 0:
                continue
            normalized = {
                "billable": billable.strip()[:100],
                "unit": unit.strip().lower()[:50],
                "cost_usd": format(cost, "f"),
            }
            variant = row.get("variant")
            if isinstance(variant, str) and variant.strip():
                normalized["variant"] = variant.strip()[:100]
            if isinstance(provider, str) and provider.strip():
                normalized["provider"] = provider.strip()[:100]
            result.append(normalized)
    return result


def _model_endpoint_url(model_id: str) -> str | None:
    parts = model_id.split("/", 1)
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        return None
    encoded = "/".join(quote(part, safe="") for part in parts)
    return f"{_base_url()}/images/models/{encoded}/endpoints"


def _fetch_model_pricing(
    client: requests.Session,
    model_id: str,
    *,
    timeout: float = _MODEL_ENDPOINT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch one model's pricing without making catalog discovery fail closed."""

    url = _model_endpoint_url(model_id)
    if url is None:
        return {}
    try:
        response = client.get(url, timeout=timeout)
    except requests.RequestException:
        logger.info("OpenRouter image pricing unavailable for %s", model_id)
        return {}
    if response.status_code != 200:
        logger.info(
            "OpenRouter image pricing returned status %s for %s",
            response.status_code,
            model_id,
        )
        return {}
    try:
        payload = response.json()
    except (TypeError, ValueError):
        logger.info("OpenRouter image pricing returned invalid JSON for %s", model_id)
        return {}
    rows = _endpoint_pricing_catalog(payload)
    if not rows:
        return {}
    return {
        "currency": "USD",
        "source": "openrouter",
        "catalog": rows,
    }


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
    specs = _parse_catalog(payload)
    enriched: list[ModelSpec] = []
    pricing_deadline = time.monotonic() + _CATALOG_PRICING_BUDGET_SECONDS
    for spec in specs:
        pricing = spec.provider_pricing
        if not pricing and spec.supports_generate:
            remaining = pricing_deadline - time.monotonic()
            if remaining > 0:
                pricing = _fetch_model_pricing(
                    client,
                    spec.model_id,
                    timeout=min(_MODEL_ENDPOINT_TIMEOUT_SECONDS, remaining),
                )
        enriched.append(
            replace(spec, provider_pricing=pricing) if pricing else spec
        )
    return enriched


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
                timeout=_CATALOG_REFRESH_WAIT_SECONDS,
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
        self._usage_events: list[dict[str, Any]] = []
        self.session.headers.update(_request_headers(require_key=True))
        self.session.headers.setdefault("Content-Type", "application/json")

    def supports_edit(self) -> bool:
        return self.spec.supports_edit

    def supports_reference(self) -> bool:
        return self.spec.supports_reference

    def usage_snapshot(self) -> dict[str, Any]:
        return merge_usage(self._usage_events)

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
        deadline = time.monotonic() + float(request_timeout)
        response: requests.Response | None = None
        for attempt in range(len(_TRANSIENT_RESPONSE_DELAYS_SECONDS) + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ImageProviderError(
                    code=CODE_UNAVAILABLE,
                    message="OpenRouter Images не ответил вовремя.",
                    http_status=504,
                )
            try:
                response = self.session.post(
                    f"{_base_url()}/images",
                    json=payload,
                    timeout=remaining,
                )
            except requests.RequestException as exc:
                # A transport error can happen after the provider accepted a
                # paid request. Retrying it could create a second charge.
                logger.warning(
                    "OpenRouter Images transport failed: model=%s error_type=%s",
                    self.model_id,
                    type(exc).__name__,
                )
                raise _network_error() from exc
            if response.status_code == 200:
                break
            error = _http_error(response)
            error_type, provider_code = _provider_error_tokens(response)
            logger.warning(
                "OpenRouter Images request rejected: model=%s status=%s "
                "attempt=%s error_type=%s provider_code=%s "
                "body_length=%s body_hash=%s",
                self.model_id,
                error.provider_status,
                attempt + 1,
                error_type or "unknown",
                provider_code or "unknown",
                error.provider_body_length,
                error.provider_body_hash,
            )
            if (
                response.status_code not in _TRANSIENT_RESPONSE_STATUSES
                or attempt >= len(_TRANSIENT_RESPONSE_DELAYS_SECONDS)
            ):
                raise error
            delay = _TRANSIENT_RESPONSE_DELAYS_SECONDS[attempt]
            if time.monotonic() + delay >= deadline:
                raise error
            time.sleep(delay)
        if response is None:
            raise _network_error()
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
        usage = normalized_response_usage(response_payload)
        if usage:
            self._usage_events.append(usage)
        try:
            return _extract_image_api(response_payload)
        except ImageProviderError as error:
            # The request succeeded upstream even when image decoding failed.
            # Durable callers need this boundary to avoid refunding paid output.
            error.provider_status = response.status_code
            raise

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
            "model": self.model_id,
            "prompt": prompt.strip(),
        }
        if n > 1:
            payload["n"] = n
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
        return self.generate_with_references(
            prompt, [image_bytes], variant_count=variant_count,
            timeout=timeout, **kwargs,
        )

    def generate_with_references(
        self, prompt: str, images: list[bytes], *, variant_count: int = 1,
        timeout: float | None = None, **kwargs: Any,
    ) -> list[bytes]:
        if not self.spec.supports_reference:
            raise ImageProviderError(
                code=CODE_IMAGE_INPUT_NOT_SUPPORTED,
                message=f"Модель '{self.spec.label}' не поддерживает референсы.",
                http_status=400,
            )
        maximum = _number_bound(
            self.spec.supported_parameters.get("input_references"), "max",
        )
        if not images or len(images) > (maximum if maximum is not None else 1):
            raise ImageProviderError(
                code=CODE_IMAGE_INPUT_NOT_SUPPORTED,
                message="Количество референсов превышает возможности модели.",
                http_status=400,
            )
        try:
            normalized_images = [normalize_image_bytes(image) for image in images]
        except StorageGatewayError as exc:
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="Референс не является допустимым изображением.",
                http_status=400,
            ) from exc
        n = _variant_count(self.spec, variant_count)
        payload: dict[str, Any] = {
            "model": self.model_id,
            "prompt": prompt.strip() if isinstance(prompt, str) else prompt,
            "input_references": [
                {"type": "image_url", "image_url": {"url": (
                    f"data:{image.mime_type};base64,"
                    f"{base64.b64encode(image.data).decode('ascii')}"
                )}} for image in normalized_images
            ],
        }
        if n > 1:
            payload["n"] = n
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
