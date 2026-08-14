"""Sanitized provider-cost collection shared by all image generation domains."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _number(value: Any) -> str | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return format(parsed, "f")


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def normalized_response_usage(response: Any) -> dict[str, Any]:
    """Extract only cost and token counters; never retain prompts or output."""

    usage = _get(response, "usage") or _get(response, "usage_metadata") or {}
    hidden = _get(response, "_hidden_params") or {}
    cost = _first_present(
        _get(usage, "cost"),
        _get(hidden, "response_cost"),
        _get(response, "response_cost"),
    )
    result: dict[str, Any] = {}
    normalized_cost = _number(cost)
    if normalized_cost is not None:
        result["costUsd"] = normalized_cost
        result["costSource"] = "provider"
    for public, aliases in {
        "promptTokens": ("prompt_tokens", "promptTokenCount"),
        "completionTokens": ("completion_tokens", "candidatesTokenCount"),
        "totalTokens": ("total_tokens", "totalTokenCount"),
    }.items():
        for alias in aliases:
            value = _get(usage, alias)
            if isinstance(value, int) and value >= 0:
                result[public] = value
                break
    return result


def merge_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    cost = Decimal("0")
    has_cost = False
    totals = {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0}
    for event in events:
        value = _number(event.get("costUsd"))
        if value is not None:
            cost += Decimal(value)
            has_cost = True
        for key in totals:
            raw = event.get(key)
            if isinstance(raw, int) and raw >= 0:
                totals[key] += raw
    result: dict[str, Any] = {key: value for key, value in totals.items() if value}
    if has_cost:
        result["costUsd"] = format(cost, "f")
        result["costSource"] = "provider"
    result["calls"] = len(events)
    return result


def provider_usage_snapshot(provider: Any) -> dict[str, Any]:
    getter = getattr(provider, "usage_snapshot", None)
    if callable(getter):
        value = getter()
        return dict(value) if isinstance(value, Mapping) else {}
    nested = getattr(provider, "provider", None)
    if nested is not None and nested is not provider:
        return provider_usage_snapshot(nested)
    return {}
