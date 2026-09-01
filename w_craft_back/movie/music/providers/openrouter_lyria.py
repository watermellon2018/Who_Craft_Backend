"""OpenRouter streaming adapter for Google Lyria 3 music generation."""

from __future__ import annotations

from decimal import Decimal
import json
from time import monotonic
from typing import Any, Iterator, Mapping
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


_CHAT_COMPLETIONS_PATH = "/chat/completions"
_LIST_PRICES = {
    "lyria-3-pro-preview": Decimal("0.08"),
    "lyria-3-clip-preview": Decimal("0.04"),
}
_EFFECTIVE_PRICES = {
    "lyria-3-pro-preview": Decimal("0.0844"),
    "lyria-3-clip-preview": Decimal("0.0422"),
}
_CREDIT_FEE = Decimal("0.055")
_ENVELOPE_OVERHEAD_BYTES = 256 * 1024


class OpenRouterLyriaProvider(AudioProvider):
    """Generate Lyria audio through OpenRouter's chat completion stream."""

    name = "openrouter-lyria"

    def __init__(
        self,
        *,
        model_name: str,
        session: requests.Session | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        route_model_id = str(model_name or "").strip()
        lyria_model_name = (
            route_model_id.removeprefix("google/")
            if route_model_id.startswith("google/")
            else route_model_id
        )
        try:
            self.lyria_model_name = validate_lyria_model(lyria_model_name)
        except ValueError as exc:
            raise self._configuration_error(str(exc)) from exc
        self.model_name = f"google/{self.lyria_model_name}"
        self.session = session or requests.Session()
        self.api_key = str(
            api_key
            if api_key is not None
            else getattr(settings, "OPENROUTER_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise self._configuration_error("OPENROUTER_API_KEY is not configured.")
        configured_base_url = str(
            base_url
            or getattr(
                settings,
                "MUSIC_OPENROUTER_API_BASE_URL",
                "https://openrouter.ai/api/v1",
            )
        ).rstrip("/")
        if not self._origin_allowed(configured_base_url, explicit=base_url is not None):
            raise self._configuration_error(
                "The configured OpenRouter music API origin is not allowed."
            )
        self.base_url = configured_base_url
        self.timeout_seconds = max(
            1.0,
            float(getattr(settings, "MUSIC_OPENROUTER_TIMEOUT_SECONDS", 180)),
        )
        self.response_deadline_seconds = min(
            900.0,
            max(
                1.0,
                float(
                    getattr(
                        settings,
                        "MUSIC_OPENROUTER_RESPONSE_DEADLINE_SECONDS",
                        300,
                    )
                ),
            ),
        )
        self.max_output_bytes = max(
            1,
            int(getattr(settings, "MUSIC_MAX_OUTPUT_BYTES", 50 * 1024 * 1024)),
        )
        self.http_referer = str(
            getattr(settings, "OPENROUTER_HTTP_REFERER", "")
        ).strip()
        self.app_title = str(getattr(settings, "OPENROUTER_APP_TITLE", "")).strip()

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
        return parsed.hostname == "openrouter.ai" and parsed.path == "/api/v1"

    def capabilities(self) -> AudioProviderCapabilities:
        minimum, maximum = (
            (3, 180)
            if self.lyria_model_name == "lyria-3-pro-preview"
            else (30, 30)
        )
        return AudioProviderCapabilities(
            provider_name=self.name,
            provider_display_name="Google Lyria 3 via OpenRouter",
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
                "OpenRouter Lyria supports exactly one variant per request.",
                code="MUSIC_CAPABILITY_UNSUPPORTED",
                http_status=400,
                retryable=False,
            )
        list_price = _LIST_PRICES[self.lyria_model_name]
        effective_price = _EFFECTIVE_PRICES[self.lyria_model_name]
        return AudioProviderPricing(
            estimated_cost=effective_price,
            snapshot={
                "currency": "USD",
                "source": "openrouter",
                "modelName": self.model_name,
                "variantCount": 1,
                "listPriceUsd": str(list_price),
                "creditFeeRate": str(_CREDIT_FEE),
                "unitCostUsd": str(effective_price),
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
            prompt = build_lyria_prompt(
                request,
                model_name=self.lyria_model_name,
            )
        except ValueError as exc:
            raise MusicProviderError(
                "The request is unsupported by OpenRouter Lyria.",
                code="MUSIC_CAPABILITY_UNSUPPORTED",
                http_status=400,
                retryable=False,
            ) from exc
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.http_referer:
            headers["HTTP-Referer"] = self.http_referer
        if self.app_title:
            headers["X-Title"] = self.app_title
        context.checkpoint()
        try:
            response = self.session.post(
                f"{self.base_url}{_CHAT_COMPLETIONS_PATH}",
                headers=headers,
                json={
                    "model": self.model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "modalities": ["text", "audio"],
                    "stream": True,
                },
                allow_redirects=False,
                stream=True,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise self._unknown_error(
                "OpenRouter Lyria submission outcome is unknown."
            ) from exc
        try:
            context.checkpoint()
            self._validate_status(response)
            return self._parse_stream(response, request, context)
        finally:
            response.close()

    def _parse_stream(
        self,
        response: requests.Response,
        request: Mapping[str, Any],
        context: ExecutionContextProtocol,
    ) -> ProviderSubmission:
        audio_parts: list[str] = []
        text_parts: list[str] = []
        request_id = str(response.headers.get("x-request-id") or "")[:200]
        completed = False
        deadline = monotonic() + self.response_deadline_seconds
        try:
            for event in self._iter_sse_events(response, context, deadline):
                if event == "[DONE]":
                    completed = True
                    break
                payload = json.loads(event)
                if not request_id and isinstance(payload, Mapping):
                    request_id = str(payload.get("id") or "")[:200]
                choices = (
                    payload.get("choices")
                    if isinstance(payload, Mapping)
                    else None
                )
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if not isinstance(choice, Mapping):
                        continue
                    part = choice.get("delta") or choice.get("message") or {}
                    if not isinstance(part, Mapping):
                        continue
                    audio = part.get("audio") or {}
                    if isinstance(audio, Mapping):
                        data = audio.get("data")
                        if data:
                            audio_parts.append(str(data))
                        transcript = audio.get("transcript")
                        if transcript:
                            text_parts.append(str(transcript))
                    content = part.get("content")
                    if isinstance(content, str) and content:
                        text_parts.append(content)
            if not completed:
                raise self._unknown_error(
                    "OpenRouter Lyria stream ended before completion.",
                    cost_incurred=True,
                )
            audio = self._decode_audio_parts(audio_parts)
        except OverflowError as exc:
            raise self._output_error("OpenRouter Lyria audio is too large.") from exc
        except MusicProviderError:
            raise
        except (
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
            requests.RequestException,
        ) as exc:
            raise self._unknown_error(
                "OpenRouter Lyria returned an invalid stream.",
                cost_incurred=True,
            ) from exc
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
                        "route": "openrouter",
                    },
                    result_snapshot={
                        "outputFormat": "mp3",
                        "transcriptSummary": transcript_summary(text_parts),
                    },
                ),
            ),
        )

    def _iter_sse_events(
        self,
        response: requests.Response,
        context: ExecutionContextProtocol,
        deadline: float,
    ) -> Iterator[str]:
        maximum = ((self.max_output_bytes + 2) // 3) * 4 + _ENVELOPE_OVERHEAD_BYTES
        total = 0
        event_data: list[str] = []
        pending = bytearray()
        consumed = 0

        def process_line(raw_line: bytes) -> str | None:
            nonlocal event_data
            line = raw_line.decode("utf-8").rstrip("\r")
            if not line:
                if event_data:
                    event = "\n".join(event_data)
                    event_data = []
                    return event
                return None
            if line.startswith(":"):
                return None
            if line.startswith("data:"):
                event_data.append(line[5:].lstrip())
            return None

        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if monotonic() >= deadline:
                    raise self._unknown_error(
                        "OpenRouter Lyria response exceeded its total deadline.",
                        cost_incurred=True,
                    )
                total += len(chunk)
                if total > maximum:
                    raise self._output_error("OpenRouter returned an oversized stream.")
                pending.extend(chunk)
                context.checkpoint()
                while True:
                    newline = pending.find(b"\n", consumed)
                    if newline < 0:
                        break
                    event = process_line(bytes(pending[consumed:newline]))
                    consumed = newline + 1
                    if event is not None:
                        yield event
                if consumed >= 64 * 1024:
                    del pending[:consumed]
                    consumed = 0
            if consumed < len(pending):
                event = process_line(bytes(pending[consumed:]))
                if event is not None:
                    yield event
            if event_data:
                yield "\n".join(event_data)
        except requests.RequestException as exc:
            raise self._unknown_error(
                "OpenRouter Lyria response stream ended unexpectedly.",
                cost_incurred=True,
            ) from exc

    def _decode_audio_parts(self, parts: list[str]) -> bytes:
        try:
            return decode_bounded_audio(
                "".join(parts),
                max_output_bytes=self.max_output_bytes,
            )
        except ValueError:
            decoded_parts = [
                decode_bounded_audio(part, max_output_bytes=self.max_output_bytes)
                for part in parts
            ]
            total = sum(len(part) for part in decoded_parts)
            if total > self.max_output_bytes:
                raise OverflowError("OpenRouter audio exceeds the byte limit.")
            if not decoded_parts:
                raise ValueError("OpenRouter returned no audio.")
            return b"".join(decoded_parts)

    def _validate_status(self, response: requests.Response) -> None:
        status_code = int(response.status_code)
        if 200 <= status_code < 300:
            return
        if 300 <= status_code < 400:
            raise MusicProviderError(
                "OpenRouter redirects are not allowed.",
                code="MUSIC_PROVIDER_REJECTED",
                http_status=502,
                retryable=False,
            )
        if status_code == 429:
            raise MusicProviderError(
                "OpenRouter rate-limited the music request.",
                code="MUSIC_PROVIDER_RATE_LIMITED",
                http_status=503,
                retryable=True,
            )
        if status_code < 500:
            raise MusicProviderError(
                "OpenRouter rejected the music request.",
                code="MUSIC_PROVIDER_REJECTED",
                http_status=400,
                retryable=False,
            )
        raise self._unknown_error(
            "OpenRouter Lyria submission outcome is unknown.",
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
