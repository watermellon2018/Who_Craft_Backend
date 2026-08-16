"""Cost-aware image-provider routing with an auditable bounded fallback."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from w_craft_back.credits.pricing import GenerationEstimate, estimate_for_spec

from .errors import (
    CODE_ERROR,
    CODE_FORBIDDEN,
    CODE_NOT_CONFIGURED,
    CODE_UNAVAILABLE,
    ImageProviderError,
)
from .registry import (
    MODEL_REGISTRY,
    ModelSpec,
    deserialize_model_spec,
    is_configured,
    resolve_model,
    serialize_model_spec,
)
from .resolver import provider_from_spec
from .usage import merge_usage, provider_usage_snapshot


ROUTING_MODES = ("manual", "economy", "fast", "balanced", "quality")
_RETRYABLE_CODES = {
    CODE_NOT_CONFIGURED,
    CODE_FORBIDDEN,
    CODE_UNAVAILABLE,
    CODE_ERROR,
}
_QUALITY = {
    "gemini-imagen-4": 5,
    "gemini-flash-image": 3,
    "openrouter-flash-image": 4,
    "gemini-native": 5,
}
_SPEED = {
    "gemini-imagen-4": 3,
    "gemini-flash-image": 1,
    "openrouter-flash-image": 2,
    "gemini-native": 2,
}


@dataclass(frozen=True)
class RoutedCandidate:
    spec: ModelSpec
    estimate: GenerationEstimate


@dataclass(frozen=True)
class RoutingDecision:
    mode: str
    reason: str
    candidates: tuple[RoutedCandidate, ...]

    @property
    def primary(self) -> RoutedCandidate:
        return self.candidates[0]

    @property
    def reservation_amount(self) -> Decimal:
        # A provider can fail after accepting the request. Reserving the sum of
        # every explicitly approved attempt keeps the fallback price bounded.
        return sum(
            (candidate.estimate.estimated_cost for candidate in self.candidates),
            Decimal("0"),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "routingMode": self.mode,
            "routingReason": self.reason,
            "reservationAmount": str(self.reservation_amount),
            "candidates": [
                {
                    "spec": serialize_model_spec(candidate.spec),
                    "estimatedCost": str(candidate.estimate.estimated_cost),
                    "pricing": candidate.estimate.snapshot,
                }
                for candidate in self.candidates
            ],
        }


def _supports(spec: ModelSpec, operation: str) -> bool:
    if not spec.supports_generate:
        return False
    if operation == "edit":
        return spec.supports_edit
    if operation == "reference":
        return spec.supports_reference
    return True


def _rank_key(candidate: RoutedCandidate, mode: str) -> tuple[Any, ...]:
    cost = candidate.estimate.estimated_cost
    quality = _QUALITY.get(candidate.spec.key, 3)
    speed = _SPEED.get(candidate.spec.key, 3)
    if mode == "economy":
        return cost, speed, -quality, candidate.spec.key
    if mode == "fast":
        return speed, cost, -quality, candidate.spec.key
    if mode == "quality":
        return -quality, cost, speed, candidate.spec.key
    return (speed + (5 - quality)), cost, speed, candidate.spec.key


def build_routing_decision(
    *,
    mode: str,
    requested_model: str | None,
    operation: str = "generate",
    variant_count: int = 1,
    prompt: str = "",
    prompt_length: int | None = None,
    resolution: str = "1K",
) -> RoutingDecision:
    """Choose a configured primary and at most one consented fallback."""

    normalized_mode = str(mode or "manual").strip().lower()
    if normalized_mode not in ROUTING_MODES:
        normalized_mode = "manual"
    estimate_kwargs = {
        "operation": "generate" if operation == "reference" else operation,
        "variant_count": variant_count,
        "prompt": prompt,
        "prompt_length": prompt_length,
        "resolution": resolution,
    }
    if normalized_mode == "manual":
        spec = resolve_model(requested_model)
        if not _supports(spec, operation):
            raise ImageProviderError(
                code="IMAGE_PROVIDER_CAPABILITY_MISMATCH",
                message="Выбранная модель не поддерживает эту операцию.",
                http_status=400,
            )
        return RoutingDecision(
            mode="manual",
            reason="explicit-model",
            candidates=(
                RoutedCandidate(spec, estimate_for_spec(spec, **estimate_kwargs)),
            ),
        )

    candidates = [
        RoutedCandidate(spec, estimate_for_spec(spec, **estimate_kwargs))
        for spec in MODEL_REGISTRY.values()
        if _supports(spec, operation) and is_configured(spec)
    ]
    if not candidates:
        raise ImageProviderError(
            code=CODE_NOT_CONFIGURED,
            message="Нет настроенной модели, подходящей для этой операции.",
            http_status=503,
        )
    candidates.sort(key=lambda candidate: _rank_key(candidate, normalized_mode))
    selected = tuple(candidates[:2])
    return RoutingDecision(
        mode=normalized_mode,
        reason=f"policy-{normalized_mode}",
        candidates=selected,
    )


def _specs_from_snapshot(snapshot: Mapping[str, Any]) -> tuple[ModelSpec, ...]:
    raw_candidates = snapshot.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("Routing snapshot candidates must be a non-empty list")
    specs: list[ModelSpec] = []
    for raw in raw_candidates:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("spec"), Mapping):
            raise ValueError("Routing snapshot candidate is invalid")
        specs.append(deserialize_model_spec(raw["spec"]))
    return tuple(specs)


class RoutedImageProvider:
    """Try only the provider attempts persisted in an approved route snapshot."""

    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self.route_snapshot = dict(snapshot)
        self.specs = _specs_from_snapshot(snapshot)
        self.spec = self.specs[0]
        self.name = self.spec.backend
        self.model_id = self.spec.model_id
        self._usage_events: list[dict[str, Any]] = []
        self._attempts: list[dict[str, str]] = []

    def supports_edit(self) -> bool:
        return all(spec.supports_edit for spec in self.specs)

    def supports_reference(self) -> bool:
        return all(spec.supports_reference for spec in self.specs)

    def _invoke(self, method: str, *args: Any, **kwargs: Any):
        last_error: ImageProviderError | None = None
        for index, spec in enumerate(self.specs):
            provider = provider_from_spec(spec)
            self.spec = spec
            self.name = spec.backend
            self.model_id = spec.model_id
            try:
                result = getattr(provider, method)(*args, **kwargs)
            except ImageProviderError as error:
                usage = provider_usage_snapshot(provider)
                if usage:
                    self._usage_events.append(usage)
                self._attempts.append({
                    "modelKey": spec.key,
                    "provider": spec.backend,
                    "result": "failed",
                    "errorCode": error.code,
                })
                last_error = error
                if error.code not in _RETRYABLE_CODES or index == len(self.specs) - 1:
                    raise
                continue
            usage = provider_usage_snapshot(provider)
            if usage:
                self._usage_events.append(usage)
            self._attempts.append({
                "modelKey": spec.key,
                "provider": spec.backend,
                "result": "succeeded",
                "errorCode": "",
            })
            return result
        if last_error is not None:
            raise last_error
        raise ImageProviderError(
            code=CODE_NOT_CONFIGURED,
            message="Маршрут генерации не содержит доступных провайдеров.",
            http_status=503,
        )

    def generate(self, *args: Any, **kwargs: Any):
        return self._invoke("generate", *args, **kwargs)

    def generate_with_reference(self, *args: Any, **kwargs: Any):
        return self._invoke("generate_with_reference", *args, **kwargs)

    def edit(self, *args: Any, **kwargs: Any):
        return self._invoke("edit", *args, **kwargs)

    def usage_snapshot(self) -> dict[str, Any]:
        usage = merge_usage(self._usage_events)
        return {
            **usage,
            "routingMode": self.route_snapshot.get("routingMode", "manual"),
            "selectedProvider": self.name,
            "selectedModel": self.model_id,
            "attempts": list(self._attempts),
        }


def provider_from_route_snapshot(snapshot: Mapping[str, Any]):
    if snapshot.get("candidates"):
        return RoutedImageProvider(snapshot)
    spec_snapshot = snapshot.get("spec", snapshot)
    return provider_from_spec(deserialize_model_spec(spec_snapshot))


def estimate_route_snapshot(
    snapshot: Mapping[str, Any],
    *,
    operation: str,
    variant_count: int,
    prompt: str,
    resolution: str = "1K",
) -> tuple[GenerationEstimate, Decimal, dict[str, Any]]:
    """Reprice a persisted route for the concrete immutable job payload."""

    specs = _specs_from_snapshot(snapshot)
    candidates = tuple(
        RoutedCandidate(
            spec,
            estimate_for_spec(
                spec,
                operation="generate" if operation == "reference" else operation,
                variant_count=variant_count,
                prompt=prompt,
                resolution=resolution,
            ),
        )
        for spec in specs
    )
    decision = RoutingDecision(
        mode=str(snapshot.get("routingMode") or "manual"),
        reason=str(snapshot.get("routingReason") or "persisted-route"),
        candidates=candidates,
    )
    return decision.primary.estimate, decision.reservation_amount, decision.snapshot()


def routing_candidate_payloads(decision: RoutingDecision) -> list[dict[str, str]]:
    return [
        {
            "modelKey": candidate.spec.key,
            "modelName": candidate.spec.model_id,
            "provider": candidate.spec.backend,
            "estimatedCost": str(candidate.estimate.estimated_cost),
        }
        for candidate in decision.candidates
    ]
