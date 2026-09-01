"""Direct Google Gemini API adapter for Lyria 3 music generation."""

from __future__ import annotations

from decimal import Decimal
import json
from time import monotonic
from typing import Any, Mapping
from urllib.parse import urlsplit

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
from .lyria_prompt import (
    build_lyria_prompt,
    decode_bounded_audio,
    transcript_summary,
    validate_lyria_model,
)


_INTERACTIONS_PATH = "/v1beta/interactions"
_DIRECT_PRICES = {
    "lyria-3-pro-preview": Decimal("0.08"),
    "lyria-3-clip-preview": Decimal("0.04"),
}
_ENVELOPE_OVERHEAD_BYTES = 256 * 1024


class GoogleLyriaProvider(AudioProvider):
    """Generate Lyria audio directly through Google's Interactions API."""

    name = "google-lyria"

    def __init__(
        self,
        *,
        model_name: str,
        session: requests.Session | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        try:
            self.model_name = validate_lyria_model(model_name)
        except ValueError as exc:
            raise self._configuration_error(str(exc)) from exc
        self.session = session or requests.Session()
        self.api_key = str(
            api_key
            if api_key is not None
            else getattr(settings, "GEMINI_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise self._configuration_error("GEMINI_API_KEY is not configured.")
        configured_base_url = str(
            base_url
            or getattr(
                settings,
                "MUSIC_GEMINI_API_BASE_URL",
                "https://generativelanguage.googleapis.com",
            )
        ).rstrip("/")
        if not self._origin_allowed(configured_base_url, explicit=base_url is not None):
            raise self._configuration_error(
                "The configured Google music API origin is not allowed."
            )
        self.base_url = configured_base_url
        self.timeout_seconds = max(
            1.0,
            float(getattr(settings, "MUSIC_GEMINI_TIMEOUT_SECONDS", 180)),
        )
        self.response_deadline_seconds = min(
            900.0,
            max(
                1.0,
                float(
                    getattr(
                        settings,
                        "MUSIC_GEMINI_RESPONSE_DEADLINE_SECONDS",
                        300,
                    )
                ),
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

    @staticmethod
    def _origin_allowed(value: str, *, explicit: bool) -> bool:
        parsed = urlsplit(value)
        try:
            secure_origin = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.port is None
                and not parsed.query
                and not parsed.fragment
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            return False
        if not secure_origin:
            return False
        if explicit:
            return True
        return (
            parsed.hostname == "generativelanguage.googleapis.com"
            and not parsed.path
        )

    def capabilities(self) -> AudioProviderCapabilities:
        minimum, maximum = (
            (3, 180)
            if self.model_name == "lyria-3-pro-preview"
            else (30, 30)
        )
        return AudioProviderCapabilities(
            provider_name=self.name,
            provider_display_name="Google Lyria 3",
            model_name=self.model_name,
            content_modes=("instrumental", "song"),
            variant_counts=(1,),
            min_duration_seconds=minimum,
            max_duration_seconds=maximum,
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

    def pricing(self, variant_count: int) -> AudioProviderPricing:
        if int(variant_count) != 1:
            raise MusicProviderError(
                "Google Lyria supports exactly one variant per request.",
                code="MUSIC_CAPABILITY_UNSUPPORTED",
                http_status=400,
                retryable=False,
            )
        price = _DIRECT_PRICES[self.model_name]
        return AudioProviderPricing(
            estimated_cost=price,
            snapshot={
                "currency": "USD",
                "source": "google-direct",
                "modelName": self.model_name,
                "variantCount": 1,
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
            prompt = build_lyria_prompt(request, model_name=self.model_name)
        except ValueError as exc:
            raise MusicProviderError(
                "The request is unsupported by Google Lyria.",
                code="MUSIC_CAPABILITY_UNSUPPORTED",
                http_status=400,
                retryable=False,
            ) from exc
        context.checkpoint()
        try:
            response = self.session.post(
                f"{self.base_url}{_INTERACTIONS_PATH}",
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
                json={"model": self.model_name, "input": prompt},
                allow_redirects=False,
                stream=True,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise self._unknown_error(
                "Google Lyria submission outcome is unknown."
            ) from exc
        try:
            context.checkpoint()
            self._validate_status(response)
            raw = self._read_response(response, context)
            return self._parse_response(raw, response, request)
        finally:
            response.close()

    def _read_response(
        self,
        response: requests.Response,
        context: ExecutionContextProtocol,
    ) -> bytes:
        deadline = monotonic() + self.response_deadline_seconds
        maximum = ((self.max_output_bytes + 2) // 3) * 4 + _ENVELOPE_OVERHEAD_BYTES
        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                if int(raw_length) > maximum:
                    raise self._output_error("Google returned an oversized response.")
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if monotonic() >= deadline:
                    raise self._unknown_error(
                        "Google Lyria response exceeded its total deadline.",
                        cost_incurred=True,
                    )
                total += len(chunk)
                if total > maximum:
                    raise self._output_error("Google returned an oversized response.")
                chunks.append(chunk)
                context.checkpoint()
        except requests.RequestException as exc:
            raise self._unknown_error(
                "Google Lyria response stream ended unexpectedly.",
                cost_incurred=True,
            ) from exc
        return b"".join(chunks)

    def _parse_response(
        self,
        raw: bytes,
        response: requests.Response,
        request: Mapping[str, Any],
    ) -> ProviderSubmission:
        try:
            payload = json.loads(raw.decode("utf-8"))
            steps = payload["steps"]
            if not isinstance(steps, list):
                raise ValueError("steps must be a list")
            audio_data = ""
            text_parts: list[str] = []
            for step in steps:
                if not isinstance(step, Mapping) or step.get("type") != "model_output":
                    continue
                content = step.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, Mapping):
                        continue
                    if item.get("type") == "audio" and not audio_data:
                        audio_data = str(item.get("data") or "")
                    elif item.get("type") == "text":
                        text_parts.append(
                            str(item.get("text") or item.get("data") or "")
                        )
            audio = decode_bounded_audio(
                audio_data,
                max_output_bytes=self.max_output_bytes,
            )
        except OverflowError as exc:
            raise self._output_error("Google Lyria audio is too large.") from exc
        except (
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise self._unknown_error(
                "Google Lyria returned an invalid response.",
                cost_incurred=True,
            ) from exc
        request_id = str(
            response.headers.get("x-request-id")
            or (payload.get("id") if isinstance(payload, Mapping) else "")
            or ""
        )[:200]
        summary = transcript_summary(text_parts)
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
                    result_snapshot={
                        "outputFormat": "mp3",
                        "transcriptSummary": summary,
                    },
                ),
            ),
        )

    def _validate_status(self, response: requests.Response) -> None:
        status_code = int(response.status_code)
        if 200 <= status_code < 300:
            return
        if 300 <= status_code < 400:
            raise MusicProviderError(
                "Google Lyria redirects are not allowed.",
                code="MUSIC_PROVIDER_REJECTED",
                http_status=502,
                retryable=False,
            )
        if status_code == 429:
            raise MusicProviderError(
                "Google Lyria rate-limited the music request.",
                code="MUSIC_PROVIDER_RATE_LIMITED",
                http_status=503,
                retryable=True,
            )
        if status_code < 500:
            raise MusicProviderError(
                "Google Lyria rejected the music request.",
                code="MUSIC_PROVIDER_REJECTED",
                http_status=400,
                retryable=False,
            )
        raise self._unknown_error(
            "Google Lyria submission outcome is unknown.",
        )

    @staticmethod
    def _unknown_error(
        message: str,
        *,
        code: str = "MUSIC_PROVIDER_OUTCOME_UNKNOWN",
        cost_incurred: bool = False,
    ) -> MusicProviderError:
        return MusicProviderError(
            message,
            code=code,
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
