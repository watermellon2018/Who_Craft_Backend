"""Bounded ElevenLabs Sound Effects v2 adapter."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests
from django.conf import settings

from w_craft_back.movie.sound_effects.errors import SoundEffectProviderError


MODEL_NAME = "eleven_text_to_sound_v2"
MODEL_KEY = "elevenlabs-sound-effects-v2"
_PATH = "/v1/sound-generation"
_MAX_RESPONSE_BYTES = 50 * 1024 * 1024


def _decimal_setting(name: str, default: str) -> Decimal:
    try:
        value = Decimal(str(getattr(settings, name, default)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SoundEffectProviderError(
            f"{name} is invalid.",
            code="SOUND_EFFECT_PRICE_UNAVAILABLE",
            retryable=False,
        ) from exc
    if not value.is_finite() or value <= 0:
        raise SoundEffectProviderError(
            f"{name} must be positive.",
            code="SOUND_EFFECT_PRICE_UNAVAILABLE",
            retryable=False,
        )
    return value


@dataclass(frozen=True)
class SoundEffectPricing:
    estimated_cost: Decimal
    snapshot: Mapping[str, Any]


@dataclass(frozen=True)
class GeneratedSoundEffect:
    payload: bytes
    mime_type: str
    provider_request_id: str
    provenance: Mapping[str, Any]


class ElevenLabsSoundEffectsProvider:
    """Generate one MP3 effect with no provider fallback."""

    name = "elevenlabs-sfx"
    model_name = MODEL_NAME
    model_key = MODEL_KEY

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.api_key = str(
            api_key
            if api_key is not None
            else getattr(settings, "ELEVENLABS_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise SoundEffectProviderError(
                "ELEVENLABS_API_KEY is not configured.",
                code="SOUND_EFFECT_PROVIDER_NOT_CONFIGURED",
                retryable=False,
            )
        configured_base = str(
            base_url
            or getattr(
                settings,
                "SOUND_EFFECTS_ELEVENLABS_API_BASE_URL",
                "https://api.elevenlabs.io",
            )
        ).rstrip("/")
        parsed = urlsplit(configured_base)
        try:
            official = (
                parsed.scheme == "https"
                and parsed.hostname == "api.elevenlabs.io"
                and parsed.port is None
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and not parsed.username
                and not parsed.password
            )
            explicit_test = (
                base_url is not None
                and parsed.scheme == "https"
                and bool(parsed.hostname)
                and not parsed.query
                and not parsed.fragment
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            official = explicit_test = False
        if not (official or explicit_test):
            raise SoundEffectProviderError(
                "The ElevenLabs API origin is not allowed.",
                code="SOUND_EFFECT_PROVIDER_NOT_CONFIGURED",
                retryable=False,
            )
        self.base_url = configured_base
        self.timeout = max(
            1.0,
            float(getattr(settings, "SOUND_EFFECTS_ELEVENLABS_TIMEOUT_SECONDS", 60)),
        )
        self.response_deadline = max(
            1.0,
            float(
                getattr(
                    settings,
                    "SOUND_EFFECTS_ELEVENLABS_RESPONSE_DEADLINE_SECONDS",
                    180,
                )
            ),
        )
        self.output_format = str(
            getattr(
                settings,
                "SOUND_EFFECTS_ELEVENLABS_OUTPUT_FORMAT",
                "mp3_44100_128",
            )
        ).strip()
        if self.output_format != "mp3_44100_128":
            raise SoundEffectProviderError(
                "Only MP3 output is supported.",
                code="SOUND_EFFECT_PROVIDER_NOT_CONFIGURED",
                retryable=False,
            )

    def pricing(self, duration_seconds: float | None) -> SoundEffectPricing:
        per_minute = _decimal_setting(
            "SOUND_EFFECTS_ELEVENLABS_COST_USD_PER_MINUTE",
            "0.12",
        )
        if duration_seconds is None:
            cost = _decimal_setting(
                "SOUND_EFFECTS_ELEVENLABS_AUTO_COST_USD",
                "",
            )
            source = "configured-auto-reservation"
        else:
            seconds = Decimal(str(duration_seconds))
            cost = per_minute * seconds / Decimal("60")
            source = "duration-rate"
        return SoundEffectPricing(
            estimated_cost=cost,
            snapshot={
                "currency": "USD",
                "source": "elevenlabs",
                "pricingMode": source,
                "modelKey": self.model_key,
                "modelName": self.model_name,
                "durationSeconds": duration_seconds,
                "costUsdPerMinute": str(per_minute),
                "estimatedCostUsd": str(cost),
                "creditUsdRate": "1",
                "markup": "0",
            },
        )

    def provider_snapshot(self, duration_seconds: float | None) -> dict[str, Any]:
        pricing = self.pricing(duration_seconds)
        return {
            "version": "sound-effect-provider-v1",
            "modelKey": self.model_key,
            "backendProvider": self.name,
            "providerDisplayName": "ElevenLabs Sound Effects",
            "modelName": self.model_name,
            "pricing": dict(pricing.snapshot),
            "estimatedCostUsd": str(pricing.estimated_cost),
            "capabilities": {
                "duration": {
                    "autoSupported": True,
                    "minSeconds": 0.5,
                    "maxSeconds": 30,
                },
                "supportsLoop": True,
                "promptInfluence": {"min": 0, "max": 1, "default": 0.3},
                "outputFormats": ["mp3"],
            },
        }

    def generate(self, request: Mapping[str, Any], context) -> GeneratedSoundEffect:
        body: dict[str, Any] = {
            "text": str(request["prompt"]),
            "model_id": self.model_name,
            "loop": bool(request.get("loop", False)),
            "prompt_influence": float(request.get("promptInfluence", 0.3)),
        }
        duration = request.get("durationSeconds")
        if duration is not None:
            body["duration_seconds"] = float(duration)
        context.checkpoint()
        started = time.monotonic()
        response = None
        try:
            response = self.session.post(
                f"{self.base_url}{_PATH}",
                params={"output_format": self.output_format},
                headers={
                    "xi-api-key": self.api_key,
                    "Accept": "audio/mpeg",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=(10, min(self.timeout, self.response_deadline)),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise SoundEffectProviderError(
                "ElevenLabs POST outcome is unknown.",
                code="SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
                http_status=502,
                retryable=False,
                outcome_unknown=True,
                cost_incurred=True,
            ) from exc
        try:
            if 300 <= response.status_code < 400:
                raise SoundEffectProviderError(
                    "ElevenLabs redirects are not accepted.",
                    code="SOUND_EFFECT_PROVIDER_REJECTED",
                    http_status=502,
                    retryable=False,
                )
            if response.status_code == 429:
                raise SoundEffectProviderError(
                    "ElevenLabs rate limit was reached.",
                    code="SOUND_EFFECT_PROVIDER_RATE_LIMITED",
                    retryable=True,
                )
            if response.status_code >= 500:
                raise SoundEffectProviderError(
                    "ElevenLabs POST outcome is unknown.",
                    code="SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
                    http_status=502,
                    retryable=False,
                    outcome_unknown=True,
                    cost_incurred=True,
                )
            if response.status_code >= 400:
                raise SoundEffectProviderError(
                    "ElevenLabs rejected the request.",
                    code="SOUND_EFFECT_PROVIDER_REJECTED",
                    http_status=422,
                    retryable=False,
                )
            raw_length = str(response.headers.get("Content-Length") or "").strip()
            if raw_length:
                try:
                    declared_length = int(raw_length)
                except ValueError as exc:
                    raise SoundEffectProviderError(
                        "ElevenLabs returned an invalid Content-Length.",
                        code="SOUND_EFFECT_OUTPUT_INVALID",
                        http_status=502,
                        retryable=False,
                        cost_incurred=True,
                    ) from exc
                if declared_length < 1 or declared_length > _MAX_RESPONSE_BYTES:
                    raise SoundEffectProviderError(
                        "ElevenLabs audio exceeds the byte limit.",
                        code="SOUND_EFFECT_OUTPUT_TOO_LARGE",
                        http_status=502,
                        retryable=False,
                        cost_incurred=True,
                    )
            chunks: list[bytes] = []
            size = 0
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if time.monotonic() - started > self.response_deadline:
                        raise SoundEffectProviderError(
                            "ElevenLabs response exceeded its deadline.",
                            code="SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
                            http_status=502,
                            retryable=False,
                            outcome_unknown=True,
                            cost_incurred=True,
                        )
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise SoundEffectProviderError(
                            "ElevenLabs audio exceeds the byte limit.",
                            code="SOUND_EFFECT_OUTPUT_TOO_LARGE",
                            http_status=502,
                            retryable=False,
                            cost_incurred=True,
                        )
                    chunks.append(bytes(chunk))
                    context.checkpoint()
            except requests.RequestException as exc:
                raise SoundEffectProviderError(
                    "ElevenLabs POST outcome is unknown.",
                    code="SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
                    http_status=502,
                    retryable=False,
                    outcome_unknown=True,
                    cost_incurred=True,
                ) from exc
            payload = b"".join(chunks)
            if not payload:
                raise SoundEffectProviderError(
                    "ElevenLabs returned empty audio.",
                    code="SOUND_EFFECT_OUTPUT_INVALID",
                    http_status=502,
                    retryable=False,
                    cost_incurred=True,
                )
            context.checkpoint()
            return GeneratedSoundEffect(
                payload=payload,
                mime_type="audio/mpeg",
                provider_request_id=str(
                    response.headers.get("request-id") or ""
                )[:255],
                provenance={
                    "provider": self.name,
                    "model": self.model_name,
                    "outputFormat": self.output_format,
                },
            )
        finally:
            response.close()
