"""Provider-native generation pricing without a Craft markup or price table."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from typing import Any, Mapping

from w_craft_back.services.image_generation.registry import (
    ModelSpec,
    deserialize_model_spec,
    resolve_model,
)

from .services import MONEY_QUANTUM, money


@dataclass(frozen=True)
class GenerationEstimate:
    provider: str
    model_key: str
    model_name: str
    currency: str
    estimated_cost: Decimal
    reservation_amount: Decimal
    pricing_source: str
    prompt_tokens_estimate: int
    snapshot: dict[str, Any]


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _catalog_unit_price(pricing: Mapping[str, Any], billable: str) -> Decimal | None:
    aliases = {
        "output_image": ("output_image", "image", "request"),
        "input_text_token": ("input_text_token", "prompt", "input"),
    }[billable]
    for key in aliases:
        value = _decimal(pricing.get(key))
        if value is not None:
            return value
    rows = pricing.get("catalog")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("billable") or "") not in aliases:
                continue
            value = _decimal(row.get("cost_usd", row.get("price")))
            if value is not None:
                return value
    return None


def estimate_for_spec(
    spec: ModelSpec,
    *,
    operation: str = "generate",
    variant_count: int = 1,
    prompt: str = "",
    prompt_length: int | None = None,
    resolution: str = "1K",
) -> GenerationEstimate:
    """Estimate the original provider's USD price for the selected operation."""

    count = max(1, min(int(variant_count or 1), 100))
    length = max(0, int(prompt_length if prompt_length is not None else len(prompt)))
    prompt_tokens = int(math.ceil(length / 4))
    pricing = dict(spec.provider_pricing or {})
    source = str(pricing.get("source") or spec.backend)
    if spec.backend == "mock" or spec.key == "mock":
        output_price = Decimal("0")
        input_price = Decimal("0")
    else:
        operation_prefix = "edit_" if operation == "edit" else "generate_"
        output_price = _decimal(pricing.get(f"{operation_prefix}output_image"))
        if output_price is None:
            by_resolution = pricing.get("output_image_by_resolution")
            if isinstance(by_resolution, Mapping):
                output_price = _decimal(
                    by_resolution.get(resolution)
                    or by_resolution.get(str(resolution).upper())
                    or by_resolution.get("1K")
                )
        if output_price is None:
            output_price = _catalog_unit_price(pricing, "output_image")
        input_price = _decimal(pricing.get(f"{operation_prefix}input_text_token"))
        if input_price is None:
            input_price = _catalog_unit_price(pricing, "input_text_token")
    if output_price is None:
        from .services import GenerationPriceUnavailable

        raise GenerationPriceUnavailable(
            "Провайдер не сообщил тариф выбранной модели. Генерация временно недоступна."
        )
    input_price = input_price or Decimal("0")
    estimated = money(output_price * count + input_price * prompt_tokens)
    snapshot = {
        "currency": "USD",
        "source": source,
        "modelKey": spec.key,
        "modelName": spec.model_id,
        "operation": operation,
        "variantCount": count,
        "resolution": resolution,
        "promptTokensEstimate": prompt_tokens,
        "outputImageUnitCost": str(output_price.quantize(MONEY_QUANTUM)),
        "inputTextTokenUnitCost": str(input_price),
        "markup": "0",
        "creditUsdRate": "1",
    }
    return GenerationEstimate(
        provider=spec.backend,
        model_key=spec.key,
        model_name=spec.model_id,
        currency="USD",
        estimated_cost=estimated,
        reservation_amount=estimated,
        pricing_source=source,
        prompt_tokens_estimate=prompt_tokens,
        snapshot=snapshot,
    )


def estimate_for_model_key(model_key: str, **kwargs: Any) -> GenerationEstimate:
    return estimate_for_spec(resolve_model(model_key), **kwargs)


def estimate_for_pinned_provider(
    *,
    provider: str,
    provider_snapshot: Mapping[str, Any] | None,
    model_name: str = "",
    **kwargs: Any,
) -> GenerationEstimate:
    if provider.strip().lower() == "mock":
        spec = ModelSpec(
            key="mock",
            label="Mock",
            backend="mock",
            model_id=model_name or "mock",
            mode="image",
            supports_generate=True,
            supports_edit=True,
            provider_pricing={"currency": "USD", "source": "local"},
        )
    elif provider_snapshot:
        raw_snapshot = provider_snapshot.get("spec", provider_snapshot)
        spec = deserialize_model_spec(raw_snapshot)
    else:
        spec = resolve_model(provider)
    return estimate_for_spec(spec, **kwargs)
