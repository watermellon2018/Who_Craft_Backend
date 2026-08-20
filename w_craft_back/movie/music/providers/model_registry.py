"""User-facing audio model catalog and immutable route resolution."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from django.conf import settings

from .base import AudioProviderCapabilities, AudioProviderPricing, MusicProviderError


SNAPSHOT_VERSION = "audio-provider-v1"


@dataclass(frozen=True)
class AudioRouteSpec:
    """One backend route capable of executing a user-facing audio model."""

    key: str
    backend_name: str
    provider_display_name: str
    model_id: str
    required_settings: tuple[str, ...]
    unit_cost_usd: Decimal
    pricing_source: str
    cost_setting_name: str = ""
    provider_list_unit_cost_usd: Decimal | None = None
    credit_purchase_fee_rate: Decimal | None = None

    def configured(self) -> bool:
        """Return whether all credentials required by this route are present."""

        if self.backend_name == "mock" and not bool(
            getattr(settings, "MUSIC_ALLOW_MOCK", False)
        ):
            return False
        return all(
            bool(str(getattr(settings, setting_name, "") or "").strip())
            for setting_name in self.required_settings
        )

    def effective_unit_cost(self) -> Decimal:
        """Return a positive configured price, or the immutable catalog price."""

        if not self.cost_setting_name:
            return self.unit_cost_usd
        configured = getattr(settings, self.cost_setting_name, self.unit_cost_usd)
        try:
            parsed = Decimal(str(configured))
        except (InvalidOperation, TypeError, ValueError):
            parsed = self.unit_cost_usd
        if not parsed.is_finite() or parsed <= 0:
            raise MusicProviderError(
                "The selected audio route price is not configured.",
                code="GENERATION_PRICE_UNAVAILABLE",
                http_status=503,
                retryable=False,
            )
        return parsed


@dataclass(frozen=True)
class AudioModelSpec:
    """Stable product model selected by users, independent of API routing."""

    key: str
    display_name: str
    capabilities: AudioProviderCapabilities
    routes: tuple[AudioRouteSpec, ...]
    preview: bool = False


@dataclass(frozen=True)
class ResolvedAudioModel:
    """A model plus the cheapest currently configured execution route."""

    model: AudioModelSpec
    route: AudioRouteSpec

    def pricing(self, variant_count: int) -> AudioProviderPricing:
        """Build the authoritative enqueue-time price for this route."""

        count = int(variant_count)
        unit_cost = self.route.effective_unit_cost()
        snapshot = {
            "currency": "USD",
            "source": self.route.pricing_source,
            "modelKey": self.model.key,
            "modelName": self.route.model_id,
            "routeKey": self.route.key,
            "variantCount": count,
            "unitCostUsd": str(unit_cost),
            "markup": "0",
            "creditUsdRate": "1",
        }
        if self.route.provider_list_unit_cost_usd is not None:
            snapshot["providerListUnitCostUsd"] = str(
                self.route.provider_list_unit_cost_usd
            )
        if self.route.credit_purchase_fee_rate is not None:
            snapshot["creditPurchaseFeeRate"] = str(
                self.route.credit_purchase_fee_rate
            )
        return AudioProviderPricing(
            estimated_cost=unit_cost * count,
            snapshot=snapshot,
        )

    def snapshot(self, variant_count: int) -> dict[str, Any]:
        """Serialize the immutable execution, capability, and pricing decision."""

        pricing = self.pricing(variant_count)
        return {
            "version": SNAPSHOT_VERSION,
            "modelKey": self.model.key,
            "modelDisplayName": self.model.display_name,
            "backendProvider": self.route.backend_name,
            "providerDisplayName": self.route.provider_display_name,
            "routeKey": self.route.key,
            "modelName": self.route.model_id,
            "capabilities": self.model.capabilities.as_public_dict(),
            "pricing": dict(pricing.snapshot),
            "estimatedCostUsd": str(pricing.estimated_cost),
        }


def _capabilities(
    *,
    provider_name: str,
    provider_display_name: str,
    model_name: str,
    content_modes: tuple[str, ...] = ("instrumental", "song"),
    variant_counts: tuple[int, ...] = (1,),
    min_duration_seconds: int = 3,
    max_duration_seconds: int = 180,
    output_formats: tuple[str, ...] = ("mp3",),
    lyrics_languages: tuple[str, ...] = ("ru", "en"),
    supports_audio_reference: bool = False,
    supports_seed: bool = False,
    supports_external_async: bool = False,
) -> AudioProviderCapabilities:
    return AudioProviderCapabilities(
        provider_name=provider_name,
        provider_display_name=provider_display_name,
        model_name=model_name,
        content_modes=content_modes,
        variant_counts=variant_counts,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        output_formats=output_formats,
        lyrics_languages=lyrics_languages,
        lyrics_section_types=("verse", "chorus", "bridge", "outro"),
        max_lyrics_chars=12000 if "song" in content_modes else 0,
        supports_audio_reference=supports_audio_reference,
        reference_formats=("mp3", "wav", "ogg") if supports_audio_reference else (),
        max_reference_bytes=(50 * 1024 * 1024 if supports_audio_reference else 0),
        min_reference_seconds=10 if supports_audio_reference else 0,
        max_reference_seconds=300 if supports_audio_reference else 0,
        supports_seed=supports_seed,
        supports_cancellation=False,
        supports_external_async=supports_external_async,
    )


def _catalog() -> tuple[AudioModelSpec, ...]:
    stable_minimum = max(1, int(getattr(settings, "MUSIC_MIN_DURATION_SECONDS", 3)))
    stable_maximum = max(
        stable_minimum,
        min(380, int(getattr(settings, "MUSIC_MAX_DURATION_SECONDS", 300))),
    )
    stable_format = str(
        getattr(settings, "MUSIC_STABILITY_OUTPUT_FORMAT", "mp3") or "mp3"
    ).strip().lower()
    return (
        AudioModelSpec(
            key="mock",
            display_name="Mock Audio",
            capabilities=_capabilities(
                provider_name="mock",
                provider_display_name="Music generator",
                model_name="deterministic-wav-v1",
                variant_counts=(1, 2),
                max_duration_seconds=300,
                output_formats=("wav",),
                supports_audio_reference=True,
                supports_seed=True,
            ),
            routes=(
                AudioRouteSpec(
                    key="mock-local",
                    backend_name="mock",
                    provider_display_name="Local mock",
                    model_id="deterministic-wav-v1",
                    required_settings=(),
                    unit_cost_usd=Decimal("0"),
                    pricing_source="local",
                ),
            ),
        ),
        AudioModelSpec(
            key="stable-audio-3",
            display_name="Stable Audio 3",
            capabilities=_capabilities(
                provider_name="stability",
                provider_display_name="Stability AI Stable Audio 3.0",
                model_name="stable-audio-3",
                content_modes=("instrumental",),
                min_duration_seconds=stable_minimum,
                max_duration_seconds=stable_maximum,
                output_formats=(stable_format,),
                lyrics_languages=(),
                supports_seed=True,
                supports_external_async=True,
            ),
            routes=(
                AudioRouteSpec(
                    key="stability-direct",
                    backend_name="stability",
                    provider_display_name="Stability AI",
                    model_id="stable-audio-3",
                    required_settings=("STABILITY_API_KEY",),
                    unit_cost_usd=Decimal("0.26"),
                    pricing_source="stability-ai",
                    cost_setting_name="MUSIC_STABILITY_COST_USD_PER_VARIANT",
                ),
            ),
        ),
        AudioModelSpec(
            key="lyria-3-pro",
            display_name="Google Lyria 3 Pro",
            capabilities=_capabilities(
                provider_name="google-lyria",
                provider_display_name="Google Lyria 3 Pro",
                model_name="lyria-3-pro-preview",
            ),
            routes=(
                AudioRouteSpec(
                    key="google-gemini-direct",
                    backend_name="google-lyria",
                    provider_display_name="Google Gemini API",
                    model_id="lyria-3-pro-preview",
                    required_settings=("GEMINI_API_KEY",),
                    unit_cost_usd=Decimal("0.08"),
                    pricing_source="google-gemini",
                ),
                AudioRouteSpec(
                    key="openrouter",
                    backend_name="openrouter-lyria",
                    provider_display_name="OpenRouter",
                    model_id="google/lyria-3-pro-preview",
                    required_settings=("OPENROUTER_API_KEY",),
                    unit_cost_usd=Decimal("0.0844"),
                    pricing_source="openrouter",
                    provider_list_unit_cost_usd=Decimal("0.08"),
                    credit_purchase_fee_rate=Decimal("0.055"),
                ),
            ),
            preview=True,
        ),
        AudioModelSpec(
            key="lyria-3-clip",
            display_name="Google Lyria 3 Clip",
            capabilities=_capabilities(
                provider_name="google-lyria",
                provider_display_name="Google Lyria 3 Clip",
                model_name="lyria-3-clip-preview",
                min_duration_seconds=30,
                max_duration_seconds=30,
            ),
            routes=(
                AudioRouteSpec(
                    key="google-gemini-direct",
                    backend_name="google-lyria",
                    provider_display_name="Google Gemini API",
                    model_id="lyria-3-clip-preview",
                    required_settings=("GEMINI_API_KEY",),
                    unit_cost_usd=Decimal("0.04"),
                    pricing_source="google-gemini",
                ),
                AudioRouteSpec(
                    key="openrouter",
                    backend_name="openrouter-lyria",
                    provider_display_name="OpenRouter",
                    model_id="google/lyria-3-clip-preview",
                    required_settings=("OPENROUTER_API_KEY",),
                    unit_cost_usd=Decimal("0.0422"),
                    pricing_source="openrouter",
                    provider_list_unit_cost_usd=Decimal("0.04"),
                    credit_purchase_fee_rate=Decimal("0.055"),
                ),
            ),
            preview=True,
        ),
    )


def audio_model_specs() -> tuple[AudioModelSpec, ...]:
    """Return the complete immutable user-facing model catalog."""

    return _catalog()


def get_audio_model_spec(model_key: str) -> AudioModelSpec:
    """Resolve only stable product keys; upstream model IDs are rejected."""

    normalized = str(model_key or "").strip().lower()
    for spec in _catalog():
        if spec.key == normalized:
            return spec
    raise MusicProviderError(
        "The selected audio model is unknown.",
        code="MUSIC_MODEL_UNKNOWN",
        http_status=400,
        retryable=False,
    )


def default_audio_model_key() -> str:
    """Resolve the new default setting, then map the legacy provider setting."""

    configured = str(
        getattr(settings, "MUSIC_DEFAULT_AUDIO_MODEL", "") or ""
    ).strip().lower()
    known_keys = {spec.key for spec in _catalog()}
    if configured in known_keys:
        return configured
    legacy = str(
        getattr(settings, "MUSIC_GENERATION_PROVIDER", "mock") or "mock"
    ).strip().lower()
    return {
        "mock": "mock",
        "stability": "stable-audio-3",
        "google-lyria": "lyria-3-pro",
        "openrouter-lyria": "lyria-3-pro",
    }.get(legacy, "mock")


def resolve_audio_model(model_key: str | None = None) -> ResolvedAudioModel:
    """Select the cheapest configured route before any paid request begins."""

    spec = get_audio_model_spec(model_key or default_audio_model_key())
    configured_routes = [route for route in spec.routes if route.configured()]
    if not configured_routes:
        raise MusicProviderError(
            "The selected audio model is not configured.",
            code="MUSIC_MODEL_NOT_CONFIGURED",
            http_status=503,
            retryable=False,
        )
    route = min(
        configured_routes,
        key=lambda candidate: candidate.effective_unit_cost(),
    )
    return ResolvedAudioModel(model=spec, route=route)


def resolve_legacy_audio_route(
    provider_name: str,
    model_name: str = "",
    *,
    require_configured: bool = False,
) -> ResolvedAudioModel:
    """Map legacy denormalized provider/model fields without changing route."""

    backend = str(provider_name or "").strip().lower()
    upstream = str(model_name or "").strip().lower()
    if upstream.endswith("lyria-3-clip-preview"):
        model_key = "lyria-3-clip"
    elif upstream.endswith("lyria-3-pro-preview"):
        model_key = "lyria-3-pro"
    else:
        model_key = {
            "mock": "mock",
            "stability": "stable-audio-3",
            "google-lyria": "lyria-3-pro",
            "openrouter-lyria": "lyria-3-pro",
        }.get(backend, "")
    spec = get_audio_model_spec(model_key)
    candidates = [route for route in spec.routes if route.backend_name == backend]
    if upstream:
        exact = [route for route in candidates if route.model_id.lower() == upstream]
        candidates = exact or candidates
    if not candidates:
        raise MusicProviderError(
            "The legacy audio route is unsupported.",
            code="MUSIC_PROVIDER_NOT_CONFIGURED",
            http_status=503,
            retryable=False,
        )
    if require_configured and not candidates[0].configured():
        raise MusicProviderError(
            "The legacy audio route is not configured.",
            code="MUSIC_MODEL_NOT_CONFIGURED",
            http_status=503,
            retryable=False,
        )
    return ResolvedAudioModel(model=spec, route=candidates[0])


def resolved_from_snapshot(snapshot: Mapping[str, Any]) -> ResolvedAudioModel:
    """Restore a route decision without consulting current defaults or ordering."""

    if snapshot.get("version") != SNAPSHOT_VERSION:
        raise MusicProviderError(
            "The stored audio route snapshot is unsupported.",
            code="MUSIC_PROVIDER_NOT_CONFIGURED",
            http_status=503,
            retryable=False,
        )
    pricing = dict(snapshot.get("pricing") or {})
    try:
        unit_cost = Decimal(str(pricing.get("unitCostUsd")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MusicProviderError(
            "The stored audio route price is invalid.",
            code="GENERATION_PRICE_UNAVAILABLE",
            http_status=503,
            retryable=False,
        ) from exc
    if not unit_cost.is_finite() or unit_cost < 0:
        raise MusicProviderError(
            "The stored audio route price is invalid.",
            code="GENERATION_PRICE_UNAVAILABLE",
            http_status=503,
            retryable=False,
        )
    model_key = str(snapshot.get("modelKey") or "").strip()
    route_key = str(snapshot.get("routeKey") or "").strip()
    backend = str(snapshot.get("backendProvider") or "").strip()
    model_name = str(snapshot.get("modelName") or "").strip()
    if not all((model_key, route_key, backend, model_name)):
        raise MusicProviderError(
            "The stored audio route snapshot is incomplete.",
            code="MUSIC_PROVIDER_NOT_CONFIGURED",
            http_status=503,
            retryable=False,
        )
    route = AudioRouteSpec(
        key=route_key,
        backend_name=backend,
        provider_display_name=str(snapshot.get("providerDisplayName") or backend),
        model_id=model_name,
        required_settings=(),
        unit_cost_usd=unit_cost,
        pricing_source=str(pricing.get("source") or backend),
    )
    model = AudioModelSpec(
        key=model_key,
        display_name=str(snapshot.get("modelDisplayName") or model_key),
        capabilities=capabilities_from_snapshot(snapshot),
        routes=(route,),
    )
    return ResolvedAudioModel(model=model, route=route)


def pricing_from_snapshot(snapshot: Mapping[str, Any]) -> AudioProviderPricing:
    """Restore immutable reservation pricing for retry without setting drift."""

    pricing = dict(snapshot.get("pricing") or {})
    try:
        estimated = Decimal(str(snapshot.get("estimatedCostUsd")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MusicProviderError(
            "The stored audio route price is invalid.",
            code="GENERATION_PRICE_UNAVAILABLE",
            http_status=503,
            retryable=False,
        ) from exc
    if not estimated.is_finite() or estimated < 0:
        raise MusicProviderError(
            "The stored audio route price is invalid.",
            code="GENERATION_PRICE_UNAVAILABLE",
            http_status=503,
            retryable=False,
        )
    return AudioProviderPricing(estimated_cost=estimated, snapshot=pricing)


def capabilities_from_snapshot(
    snapshot: Mapping[str, Any],
) -> AudioProviderCapabilities:
    """Restore the capability subset used to validate immutable retries."""

    payload = dict(snapshot.get("capabilities") or {})
    duration = dict(payload.get("duration") or {})
    lyrics = dict(payload.get("lyrics") or {})
    reference = dict(payload.get("audioReference") or {})
    return AudioProviderCapabilities(
        provider_name=str(snapshot.get("backendProvider") or ""),
        provider_display_name=str(snapshot.get("providerDisplayName") or ""),
        model_name=str(snapshot.get("modelName") or ""),
        content_modes=tuple(payload.get("contentModes") or ()),
        variant_counts=tuple(
            int(value) for value in payload.get("variantCounts") or ()
        ),
        min_duration_seconds=int(duration.get("minSeconds") or 0),
        max_duration_seconds=int(duration.get("maxSeconds") or 0),
        output_formats=tuple(payload.get("outputFormats") or ()),
        lyrics_languages=tuple(lyrics.get("languages") or ()),
        lyrics_section_types=tuple(lyrics.get("sectionTypes") or ()),
        max_lyrics_chars=int(lyrics.get("maxChars") or 0),
        supports_audio_reference=bool(reference.get("supported")),
        reference_formats=tuple(reference.get("formats") or ()),
        max_reference_bytes=int(reference.get("maxBytes") or 0),
        min_reference_seconds=int(reference.get("minSeconds") or 0),
        max_reference_seconds=int(reference.get("maxSeconds") or 0),
        supports_seed=bool(payload.get("supportsSeed")),
        supports_cancellation=bool(payload.get("supportsCancellation")),
    )


def public_audio_model_catalog() -> list[dict[str, Any]]:
    """Serialize product models and route availability without secret values."""

    default_key = default_audio_model_key()
    rows: list[dict[str, Any]] = []

    def route_cost(route: AudioRouteSpec) -> Decimal:
        try:
            return route.effective_unit_cost()
        except MusicProviderError:
            return route.unit_cost_usd

    for spec in _catalog():
        routes = sorted(spec.routes, key=route_cost)
        rows.append(
            {
                "key": spec.key,
                "label": spec.display_name,
                "configured": any(
                    route.configured()
                    and _route_has_valid_price(route)
                    for route in spec.routes
                ),
                "default": spec.key == default_key,
                "preview": spec.preview,
                "capabilities": spec.capabilities.as_public_dict(),
                "routes": [
                    {
                        "key": route.key,
                        "provider": route.backend_name,
                        "providerDisplayName": route.provider_display_name,
                        "configured": (
                            route.configured() and _route_has_valid_price(route)
                        ),
                        "unitCostUsd": str(route_cost(route)),
                    }
                    for route in routes
                ],
            }
        )
    return rows


def _route_has_valid_price(route: AudioRouteSpec) -> bool:
    try:
        route.effective_unit_cost()
    except MusicProviderError:
        return False
    return True
