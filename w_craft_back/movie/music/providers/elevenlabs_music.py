"""ElevenLabs Music v2 adapter for durable music generation."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
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
from .commercial_music import (
    elevenlabs_composition_plan,
    musical_direction,
    origin_allowed,
)


_MUSIC_PATH = "/v1/music"
_MODEL_NAME = "music_v2"
_OUTPUT_FORMAT = "mp3_48000_192"


class ElevenLabsMusicProvider(AudioProvider):
    """Generate songs or instrumentals through ElevenLabs Music v2."""

    name = "elevenlabs-music-v2"

    def __init__(
        self,
        *,
        model_name: str = _MODEL_NAME,
        session: requests.Session | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model_name = str(model_name or _MODEL_NAME).strip()
        if self.model_name != _MODEL_NAME:
            raise self._configuration_error(
                "The configured ElevenLabs music model is unsupported."
            )
        self.session = session or requests.Session()
        self.api_key = str(
            api_key
            if api_key is not None
            else getattr(settings, "ELEVENLABS_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise self._configuration_error(
                "ELEVENLABS_API_KEY is not configured."
            )
        configured_base_url = str(
            base_url
            or getattr(
                settings,
                "MUSIC_ELEVENLABS_API_BASE_URL",
                "https://api.elevenlabs.io",
            )
        ).rstrip("/")
        if not origin_allowed(
            configured_base_url,
            official_hostname="api.elevenlabs.io",
            explicit=base_url is not None,
        ):
            raise self._configuration_error(
                "The configured ElevenLabs music API origin is not allowed."
            )
        self.base_url = configured_base_url
        self.timeout_seconds = max(
            1.0,
            float(getattr(settings, "MUSIC_ELEVENLABS_TIMEOUT_SECONDS", 180)),
        )
        self.response_deadline_seconds = max(
            1.0,
            float(
                getattr(
                    settings,
                    "MUSIC_ELEVENLABS_RESPONSE_DEADLINE_SECONDS",
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
            600,
            int(getattr(settings, "MUSIC_MAX_DURATION_SECONDS", 300)),
        )
        return AudioProviderCapabilities(
            provider_name=self.name,
            provider_display_name="ElevenLabs Music v2",
            model_name=self.model_name,
            content_modes=("instrumental", "song"),
            variant_counts=(1,),
            min_duration_seconds=3,
            max_duration_seconds=max(3, maximum),
            output_formats=("mp3",),
            lyrics_languages=("ru", "en"),
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
        if int(variant_count) != 1 or not duration_seconds:
            raise MusicProviderError(
                "ElevenLabs pricing requires one variant and a duration.",
                code="GENERATION_PRICE_UNAVAILABLE",
                http_status=503,
                retryable=False,
            )
        try:
            rate = Decimal(
                str(
                    getattr(
                        settings,
                        "MUSIC_ELEVENLABS_COST_USD_PER_MINUTE",
                        "0.15",
                    )
                )
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise self._configuration_error(
                "The ElevenLabs music price is invalid."
            ) from exc
        if not rate.is_finite() or rate <= 0:
            raise self._configuration_error(
                "The ElevenLabs music price is invalid."
            )
        duration = int(duration_seconds)
        cost = rate * Decimal(duration) / Decimal(60)
        return AudioProviderPricing(
            estimated_cost=cost,
            snapshot={
                "currency": "USD",
                "source": "elevenlabs",
                "modelName": self.model_name,
                "variantCount": 1,
                "durationSeconds": duration,
                "billingUnit": "minute",
                "unitCostUsd": str(rate),
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
                "The request is unsupported by ElevenLabs Music v2.",
                code="MUSIC_CAPABILITY_UNSUPPORTED",
                http_status=400,
                retryable=False,
            ) from exc
        context.checkpoint()
        started_at = time.monotonic()
        try:
            response = self.session.post(
                f"{self.base_url}{_MUSIC_PATH}",
                params={"output_format": _OUTPUT_FORMAT},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                    "xi-api-key": self.api_key,
                },
                json=payload,
                allow_redirects=False,
                stream=True,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise self._unknown_error(
                "ElevenLabs music submission outcome is unknown."
            ) from exc
        try:
            self._validate_status(response)
            audio = self._read_audio(response, context, started_at)
            request_id = str(
                response.headers.get("song-id")
                or response.headers.get("x-request-id")
                or ""
            )[:200]
            return ProviderSubmission(
                outputs=(
                    GeneratedAudio(
                        payload=audio,
                        mime_type="audio/mpeg",
                        duration_seconds=None,
                        provider_request_id=request_id,
                        provenance={
                            "provider": self.name,
                            "model": self.model_name,
                            "route": "direct",
                        },
                        result_snapshot={"outputFormat": "mp3"},
                    ),
                ),
            )
        finally:
            response.close()

    def _request_payload(self, request: Mapping[str, Any]) -> dict[str, Any]:
        duration = int(request["durationSeconds"])
        if not 3 <= duration <= 600:
            raise ValueError("Duration is outside ElevenLabs limits.")
        if int(request.get("variantCount") or 1) != 1:
            raise ValueError("ElevenLabs supports one variant.")
        mode = str(request.get("contentMode") or "").strip()
        if mode == "instrumental":
            return {
                "prompt": musical_direction(request, maximum=4100),
                "music_length_ms": duration * 1000,
                "model_id": self.model_name,
                "force_instrumental": True,
            }
        if mode == "song":
            return {
                "composition_plan": elevenlabs_composition_plan(request),
                "model_id": self.model_name,
            }
        raise ValueError("Unsupported content mode.")

    def _read_audio(
        self,
        response: requests.Response,
        context: ExecutionContextProtocol,
        started_at: float,
    ) -> bytes:
        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                if int(raw_length) > self.max_output_bytes:
                    raise self._output_error(
                        "ElevenLabs audio exceeds the configured byte limit."
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
                        "ElevenLabs music response exceeded its deadline.",
                        cost_incurred=True,
                    )
                total += len(chunk)
                if total > self.max_output_bytes:
                    raise self._output_error(
                        "ElevenLabs audio exceeds the configured byte limit."
                    )
                chunks.append(chunk)
                context.checkpoint()
        except requests.RequestException as exc:
            raise self._unknown_error(
                "ElevenLabs music response ended unexpectedly.",
                cost_incurred=True,
            ) from exc
        if not chunks:
            raise self._unknown_error(
                "ElevenLabs returned no music audio.",
                cost_incurred=True,
            )
        return b"".join(chunks)

    def _validate_status(self, response: requests.Response) -> None:
        status = int(response.status_code)
        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            raise self._rejected_error("ElevenLabs redirects are not allowed.")
        if status == 429:
            raise MusicProviderError(
                "ElevenLabs rate-limited the music request.",
                code="MUSIC_PROVIDER_RATE_LIMITED",
                http_status=503,
                retryable=True,
            )
        if status < 500:
            raise self._rejected_error("ElevenLabs rejected the music request.")
        raise self._unknown_error(
            "ElevenLabs music submission outcome is unknown."
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
