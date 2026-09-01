"""Grandfathered MiniMax Music 3.0 adapter for durable music jobs."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
import time
from typing import Any, Mapping

import requests
from django.conf import settings

from .base import (
    AudioProvider,
    AudioProviderCapabilities,
    AudioProviderPricing,
    ExecutionContextProtocol,
    GeneratedAudio,
    MusicProviderError,
    ProviderSubmission,
)
from .commercial_music import formatted_lyrics, musical_direction, origin_allowed


_MUSIC_PATH = "/v1/music_generation"
_MODEL_NAME = "music-3.0"
_ENVELOPE_OVERHEAD_BYTES = 256 * 1024


class MiniMaxMusicProvider(AudioProvider):
    """Generate MiniMax Music only for explicitly confirmed legacy accounts."""

    name = "minimax-music-3"

    def __init__(
        self,
        *,
        model_name: str = _MODEL_NAME,
        session: requests.Session | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        legacy_access_confirmed: bool | None = None,
    ) -> None:
        self.model_name = str(model_name or _MODEL_NAME).strip()
        if self.model_name != _MODEL_NAME:
            raise self._configuration_error(
                "The configured MiniMax music model is unsupported."
            )
        confirmed = (
            legacy_access_confirmed
            if legacy_access_confirmed is not None
            else bool(
                getattr(
                    settings,
                    "MUSIC_MINIMAX_LEGACY_PAID_ACCESS_CONFIRMED",
                    False,
                )
            )
        )
        if not confirmed:
            raise self._configuration_error(
                "MiniMax legacy paid access is not confirmed."
            )
        self.session = session or requests.Session()
        self.api_key = str(
            api_key
            if api_key is not None
            else getattr(settings, "MINIMAX_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise self._configuration_error("MINIMAX_API_KEY is not configured.")
        configured_base_url = str(
            base_url
            or getattr(
                settings,
                "MUSIC_MINIMAX_API_BASE_URL",
                "https://api.minimax.io",
            )
        ).rstrip("/")
        if not origin_allowed(
            configured_base_url,
            official_hostname="api.minimax.io",
            explicit=base_url is not None,
        ):
            raise self._configuration_error(
                "The configured MiniMax music API origin is not allowed."
            )
        self.base_url = configured_base_url
        self.timeout_seconds = max(
            1.0,
            float(getattr(settings, "MUSIC_MINIMAX_TIMEOUT_SECONDS", 180)),
        )
        self.response_deadline_seconds = max(
            1.0,
            float(
                getattr(
                    settings,
                    "MUSIC_MINIMAX_RESPONSE_DEADLINE_SECONDS",
                    300,
                )
            ),
        )
        self.max_output_bytes = max(
            1,
            int(getattr(settings, "MUSIC_MAX_OUTPUT_BYTES", 50 * 1024 * 1024)),
        )

    @staticmethod
    def _configuration_error(message: str) -> MusicProviderError:
        return MusicProviderError(
            message,
            code="MUSIC_PROVIDER_NOT_CONFIGURED",
            http_status=503,
            retryable=False,
        )

    def capabilities(self) -> AudioProviderCapabilities:
        maximum = min(
            300,
            int(getattr(settings, "MUSIC_MAX_DURATION_SECONDS", 300)),
        )
        return AudioProviderCapabilities(
            provider_name=self.name,
            provider_display_name="MiniMax Music 3.0",
            model_name=self.model_name,
            content_modes=("instrumental", "song"),
            variant_counts=(1,),
            min_duration_seconds=3,
            max_duration_seconds=max(3, maximum),
            output_formats=("mp3",),
            lyrics_languages=("ru", "en"),
            max_lyrics_chars=3500,
            supports_audio_reference=False,
            reference_formats=(),
            max_reference_bytes=0,
            min_reference_seconds=0,
            max_reference_seconds=0,
            supports_seed=False,
            supports_cancellation=False,
            supports_external_async=False,
        )

    def pricing(
        self,
        variant_count: int,
        *,
        duration_seconds: int | None = None,
    ) -> AudioProviderPricing:
        del duration_seconds
        if int(variant_count) != 1:
            raise MusicProviderError(
                "MiniMax Music supports one variant per request.",
                code="MUSIC_CAPABILITY_UNSUPPORTED",
                http_status=400,
                retryable=False,
            )
        try:
            price = Decimal(
                str(
                    getattr(
                        settings,
                        "MUSIC_MINIMAX_COST_USD_PER_GENERATION",
                        "0.15",
                    )
                )
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise self._configuration_error(
                "The MiniMax music price is invalid."
            ) from exc
        if not price.is_finite() or price <= 0:
            raise self._configuration_error("The MiniMax music price is invalid.")
        return AudioProviderPricing(
            estimated_cost=price,
            snapshot={
                "currency": "USD",
                "source": "minimax",
                "modelName": self.model_name,
                "variantCount": 1,
                "billingUnit": "generation",
                "unitCostUsd": str(price),
                "markup": "0",
                "creditUsdRate": "1",
            },
        )

    def submit(
        self,
        request: Mapping[str, Any],
        context: ExecutionContextProtocol,
    ) -> ProviderSubmission:
        try:
            payload = self._request_payload(request)
        except (TypeError, ValueError) as exc:
            raise MusicProviderError(
                "The request is unsupported by MiniMax Music 3.0.",
                code="MUSIC_CAPABILITY_UNSUPPORTED",
                http_status=400,
                retryable=False,
            ) from exc
        context.checkpoint()
        started_at = time.monotonic()
        try:
            response = self.session.post(
                f"{self.base_url}{_MUSIC_PATH}",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                allow_redirects=False,
                stream=True,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise self._unknown_error(
                "MiniMax music submission outcome is unknown."
            ) from exc
        try:
            self._validate_status(response)
            raw = self._read_response(response, context, started_at)
            return self._parse_response(raw, request)
        finally:
            response.close()

    def _request_payload(self, request: Mapping[str, Any]) -> dict[str, Any]:
        duration = int(request["durationSeconds"])
        if not 3 <= duration <= self.capabilities().max_duration_seconds:
            raise ValueError("Duration is outside MiniMax limits.")
        if int(request.get("variantCount") or 1) != 1:
            raise ValueError("MiniMax supports one variant.")
        mode = str(request.get("contentMode") or "").strip()
        direction = musical_direction(request, maximum=1900)
        payload: dict[str, Any] = {
            "model": self.model_name,
            "prompt": (
                f"{direction}\nTarget duration: approximately {duration} seconds."
            )[:2000],
            "stream": False,
            "output_format": "hex",
            "audio_setting": {
                "sample_rate": 44100,
                "bitrate": 256000,
                "format": "mp3",
            },
            "lyrics_optimizer": False,
        }
        if mode == "instrumental":
            payload["is_instrumental"] = True
        elif mode == "song":
            payload["is_instrumental"] = False
            payload["lyrics"] = formatted_lyrics(request, maximum=3500)
        else:
            raise ValueError("Unsupported content mode.")
        return payload

    def _read_response(
        self,
        response: requests.Response,
        context: ExecutionContextProtocol,
        started_at: float,
    ) -> bytes:
        maximum = self.max_output_bytes * 2 + _ENVELOPE_OVERHEAD_BYTES
        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                if int(raw_length) > maximum:
                    raise self._output_error(
                        "MiniMax returned an oversized response."
                    )
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if time.monotonic() - started_at > self.response_deadline_seconds:
                    raise self._unknown_error(
                        "MiniMax music response exceeded its deadline.",
                        cost_incurred=True,
                    )
                total += len(chunk)
                if total > maximum:
                    raise self._output_error(
                        "MiniMax returned an oversized response."
                    )
                chunks.append(chunk)
                context.checkpoint()
        except requests.RequestException as exc:
            raise self._unknown_error(
                "MiniMax music response ended unexpectedly.",
                cost_incurred=True,
            ) from exc
        return b"".join(chunks)

    def _parse_response(
        self,
        raw: bytes,
        request: Mapping[str, Any],
    ) -> ProviderSubmission:
        try:
            payload = json.loads(raw.decode("utf-8"))
            base_response = payload["base_resp"]
            if not isinstance(base_response, Mapping):
                raise ValueError("base_resp must be an object")
            provider_status = int(base_response.get("status_code"))
            if provider_status != 0:
                raise MusicProviderError(
                    "MiniMax rejected the music request.",
                    code="MUSIC_PROVIDER_REJECTED",
                    http_status=400,
                    retryable=False,
                )
            data = payload["data"]
            if not isinstance(data, Mapping) or int(data.get("status")) != 2:
                raise ValueError("MiniMax response is not complete")
            encoded = str(data.get("audio") or "")
            if len(encoded) > self.max_output_bytes * 2:
                raise OverflowError("MiniMax audio exceeds the byte limit")
            if not re.fullmatch(r"[0-9a-fA-F]+", encoded):
                raise ValueError("MiniMax returned invalid hex audio")
            audio = bytes.fromhex(encoded)
            if len(audio) > self.max_output_bytes:
                raise OverflowError("MiniMax audio exceeds the byte limit")
            extra = payload.get("extra_info") or {}
            duration_ms = (
                int(extra.get("music_duration") or 0)
                if isinstance(extra, Mapping)
                else 0
            )
            if duration_ms < 0 or duration_ms > 600_000:
                raise ValueError("MiniMax returned an invalid music duration")
        except MusicProviderError:
            raise
        except OverflowError as exc:
            raise self._output_error("MiniMax audio is too large.") from exc
        except (
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise self._unknown_error(
                "MiniMax returned an invalid music response.",
                cost_incurred=True,
            ) from exc
        request_id = str(payload.get("trace_id") or "")[:200]
        return ProviderSubmission(
            outputs=(
                GeneratedAudio(
                    payload=audio,
                    mime_type="audio/mpeg",
                    duration_seconds=(duration_ms / 1000 if duration_ms else None),
                    provider_request_id=request_id,
                    provenance={
                        "provider": self.name,
                        "model": self.model_name,
                        "route": "direct",
                    },
                    result_snapshot={
                        "outputFormat": "mp3",
                        "providerStatus": provider_status,
                        "musicDurationMs": duration_ms,
                    },
                ),
            ),
        )

    def _validate_status(self, response: requests.Response) -> None:
        status = int(response.status_code)
        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            raise self._rejected_error("MiniMax redirects are not allowed.")
        if status == 429:
            raise MusicProviderError(
                "MiniMax rate-limited the music request.",
                code="MUSIC_PROVIDER_RATE_LIMITED",
                http_status=503,
                retryable=True,
            )
        if status < 500:
            raise self._rejected_error("MiniMax rejected the music request.")
        raise self._unknown_error(
            "MiniMax music submission outcome is unknown."
        )

    @staticmethod
    def _rejected_error(message: str) -> MusicProviderError:
        return MusicProviderError(
            message,
            code="MUSIC_PROVIDER_REJECTED",
            http_status=400,
            retryable=False,
        )

    @staticmethod
    def _unknown_error(
        message: str,
        *,
        cost_incurred: bool = False,
    ) -> MusicProviderError:
        return MusicProviderError(
            message,
            code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
            http_status=502,
            retryable=False,
            outcome_unknown=True,
            cost_incurred=cost_incurred,
        )

    @staticmethod
    def _output_error(message: str) -> MusicProviderError:
        return MusicProviderError(
            message,
            code="MUSIC_OUTPUT_TOO_LARGE",
            http_status=502,
            retryable=False,
            cost_incurred=True,
        )
