"""Stability AI Stable Audio 3.0 adapter for durable music jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
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


_TEXT_TO_AUDIO_PATH = "/v2beta/audio/stable-audio/text-to-audio"
_RESULT_PATH = "/v2beta/audio/results/{generation_id}"
_MAX_SEED = 4_294_967_294
_MAX_SUBMIT_RESPONSE_BYTES = 64 * 1024
_SUPPORTED_OUTPUT_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav"}


def _safe_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


class StabilityAudioProvider(AudioProvider):
    """Asynchronous text-to-audio integration with Stable Audio 3.0."""

    name = "stability"

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model_name: str | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.api_key = str(
            api_key
            if api_key is not None
            else getattr(settings, "STABILITY_API_KEY", "")
        ).strip()
        if not self.api_key:
            raise MusicProviderError(
                "STABILITY_API_KEY is not configured.",
                code="MUSIC_PROVIDER_NOT_CONFIGURED",
                http_status=503,
                retryable=False,
            )
        configured_base_url = str(
            base_url
            or getattr(
                settings,
                "MUSIC_STABILITY_API_BASE_URL",
                "https://api.stability.ai",
            )
        ).rstrip("/")
        parsed_base_url = urlsplit(configured_base_url)
        try:
            is_official_origin = (
                parsed_base_url.scheme == "https"
                and parsed_base_url.hostname == "api.stability.ai"
                and parsed_base_url.port is None
                and not parsed_base_url.path
                and not parsed_base_url.query
                and not parsed_base_url.fragment
                and not parsed_base_url.username
                and not parsed_base_url.password
            )
        except ValueError:
            is_official_origin = False
        is_explicit_test_origin = (
            base_url is not None
            and parsed_base_url.scheme == "https"
            and bool(parsed_base_url.hostname)
            and not parsed_base_url.query
            and not parsed_base_url.fragment
            and not parsed_base_url.username
            and not parsed_base_url.password
        )
        if not (is_official_origin or is_explicit_test_origin):
            raise MusicProviderError(
                "The configured Stability API origin is not allowed.",
                code="MUSIC_PROVIDER_NOT_CONFIGURED",
                http_status=503,
                retryable=False,
            )
        self.base_url = configured_base_url
        self.model_name = str(
            model_name
            if model_name is not None
            else getattr(settings, "MUSIC_STABILITY_MODEL", "stable-audio-3")
        ).strip()
        if self.model_name != "stable-audio-3":
            raise MusicProviderError(
                "The configured Stability music model is unsupported.",
                code="MUSIC_PROVIDER_NOT_CONFIGURED",
                http_status=503,
                retryable=False,
            )
        self.output_format = str(
            getattr(settings, "MUSIC_STABILITY_OUTPUT_FORMAT", "mp3")
        ).strip().lower()
        if self.output_format not in _SUPPORTED_OUTPUT_FORMATS:
            raise MusicProviderError(
                "The configured Stability output format is unsupported.",
                code="MUSIC_PROVIDER_NOT_CONFIGURED",
                http_status=503,
                retryable=False,
            )
        self.timeout_seconds = max(
            1.0,
            float(getattr(settings, "MUSIC_STABILITY_TIMEOUT_SECONDS", 30)),
        )
        self.poll_seconds = max(
            1.0,
            float(getattr(settings, "MUSIC_STABILITY_POLL_SECONDS", 10)),
        )
        self.max_poll_seconds = max(
            self.poll_seconds,
            float(
                getattr(settings, "MUSIC_STABILITY_MAX_POLL_SECONDS", 1800)
            ),
        )
        self.max_output_bytes = max(
            1,
            int(getattr(settings, "MUSIC_MAX_OUTPUT_BYTES", 50 * 1024 * 1024)),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "audio/*",
            "Stability-Client-ID": "who-craft",
        }

    def capabilities(self) -> AudioProviderCapabilities:
        minimum = max(
            1,
            int(getattr(settings, "MUSIC_MIN_DURATION_SECONDS", 3)),
        )
        maximum = min(
            380,
            int(getattr(settings, "MUSIC_MAX_DURATION_SECONDS", 300)),
        )
        return AudioProviderCapabilities(
            provider_name=self.name,
            provider_display_name="Stability AI Stable Audio 3.0",
            model_name=self.model_name,
            content_modes=("instrumental",),
            variant_counts=(1,),
            min_duration_seconds=minimum,
            max_duration_seconds=max(minimum, maximum),
            output_formats=(self.output_format,),
            lyrics_languages=(),
            lyrics_section_types=(),
            max_lyrics_chars=0,
            supports_audio_reference=False,
            reference_formats=(),
            max_reference_bytes=0,
            min_reference_seconds=0,
            max_reference_seconds=0,
            supports_seed=True,
            supports_cancellation=False,
            supports_external_async=True,
        )

    def pricing(self, variant_count: int) -> AudioProviderPricing:
        unit_cost = _safe_decimal(
            getattr(settings, "MUSIC_STABILITY_COST_USD_PER_VARIANT", "0.26")
        )
        if unit_cost is None or unit_cost <= 0:
            raise MusicProviderError(
                "The Stability music price is not configured.",
                code="GENERATION_PRICE_UNAVAILABLE",
                http_status=503,
                retryable=False,
            )
        count = int(variant_count)
        return AudioProviderPricing(
            estimated_cost=unit_cost * count,
            snapshot={
                "currency": "USD",
                "source": "stability-ai",
                "modelName": self.model_name,
                "variantCount": count,
                "providerCreditsPerVariant": 26,
                "unitCostUsd": str(unit_cost),
                "markup": "0",
                "creditUsdRate": "1",
            },
        )

    def submit(
        self,
        request: Mapping[str, Any],
        context: ExecutionContextProtocol,
    ) -> ProviderSubmission:
        context.checkpoint()
        prompt = str(request.get("positivePrompt") or "").strip()
        negative = str(request.get("negativePrompt") or "").strip()
        if negative:
            prompt = f"{prompt}. Avoid: {negative}"
        prompt = prompt[:10_000]
        seed = min(max(int(request.get("baseSeed") or 0), 0), _MAX_SEED)
        data = {
            "prompt": prompt,
            "model": self.model_name,
            "duration": str(int(request["durationSeconds"])),
            "seed": str(seed),
            "output_format": self.output_format,
        }
        try:
            response = self.session.post(
                f"{self.base_url}{_TEXT_TO_AUDIO_PATH}",
                headers=self._headers(),
                files={key: (None, value) for key, value in data.items()},
                allow_redirects=False,
                stream=True,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise MusicProviderError(
                "Stability submission outcome is unknown.",
                code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                http_status=502,
                retryable=False,
                outcome_unknown=True,
            ) from exc
        context.checkpoint()
        if response.status_code != 202:
            status_code = int(response.status_code)
            response.close()
            if status_code >= 500:
                raise MusicProviderError(
                    "Stability submission outcome is unknown.",
                    code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                    http_status=502,
                    retryable=False,
                    outcome_unknown=True,
                )
            self._raise_http_error(response)
        try:
            payload = self._read_submit_response(response, context)
            generation_id = str(payload.get("id") or "").strip()
        except (
            AttributeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            requests.RequestException,
        ) as exc:
            raise MusicProviderError(
                "Stability returned an invalid submission response.",
                code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                http_status=502,
                retryable=False,
                outcome_unknown=True,
            ) from exc
        if not re.fullmatch(r"[0-9a-fA-F]{64}", generation_id):
            raise MusicProviderError(
                "Stability returned no generation id.",
                code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                http_status=502,
                retryable=False,
                outcome_unknown=True,
            )
        return ProviderSubmission(
            external_job_id=generation_id,
            poll_after_seconds=self.poll_seconds,
            provider_metadata={
                "durationSeconds": int(request["durationSeconds"]),
                "outputFormat": self.output_format,
                "seed": seed,
                "pollStartedAt": datetime.now(timezone.utc).isoformat(),
                "pollCount": 0,
            },
        )

    def poll(
        self,
        external_job_id: str,
        context: ExecutionContextProtocol,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> ProviderSubmission:
        context.checkpoint()
        metadata = self._poll_metadata(provider_metadata)
        if self._poll_deadline_exceeded(metadata):
            raise MusicProviderError(
                "Stability generation exceeded the polling deadline.",
                code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                http_status=504,
                retryable=False,
                outcome_unknown=True,
            )
        metadata["pollCount"] = int(metadata.get("pollCount") or 0) + 1
        try:
            response = self.session.get(
                f"{self.base_url}{_RESULT_PATH.format(generation_id=external_job_id)}",
                headers=self._headers(),
                allow_redirects=False,
                stream=True,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException:
            return ProviderSubmission(
                external_job_id=external_job_id,
                poll_after_seconds=self.poll_seconds,
                provider_metadata=metadata,
            )
        context.checkpoint()
        if response.status_code == 202:
            response.close()
            return ProviderSubmission(
                external_job_id=external_job_id,
                poll_after_seconds=self.poll_seconds,
                provider_metadata=metadata,
            )
        if response.status_code == 429 or response.status_code >= 500:
            response.close()
            return ProviderSubmission(
                external_job_id=external_job_id,
                poll_after_seconds=(
                    max(60.0, self.poll_seconds)
                    if response.status_code == 429
                    else self.poll_seconds
                ),
                provider_metadata=metadata,
            )
        if response.status_code != 200:
            response.close()
            self._raise_http_error(response)
        result_format = str(
            metadata.get("outputFormat") or self.output_format
        ).lower()
        if result_format not in _SUPPORTED_OUTPUT_FORMATS:
            response.close()
            raise MusicProviderError(
                "Stability job metadata contains an unsupported output format.",
                code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                http_status=502,
                retryable=False,
                outcome_unknown=True,
            )
        mime_type = _SUPPORTED_OUTPUT_FORMATS[result_format]
        try:
            payload = self._read_audio(response, context)
        except requests.RequestException:
            return ProviderSubmission(
                external_job_id=external_job_id,
                poll_after_seconds=self.poll_seconds,
                provider_metadata=metadata,
            )
        return ProviderSubmission(
            outputs=(
                GeneratedAudio(
                    payload=payload,
                    mime_type=mime_type,
                    duration_seconds=None,
                    seed=(
                        int(metadata["seed"])
                        if metadata.get("seed") is not None
                        else None
                    ),
                    provider_request_id=external_job_id,
                    provenance={
                        "provider": self.name,
                        "model": self.model_name,
                        "trainingData": "licensed",
                    },
                    result_snapshot={
                        "generationId": external_job_id,
                        "outputFormat": result_format,
                    },
                ),
            ),
        )

    @staticmethod
    def _read_submit_response(
        response: requests.Response,
        context: ExecutionContextProtocol,
    ) -> Mapping[str, Any]:
        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = 0
            if content_length > _MAX_SUBMIT_RESPONSE_BYTES:
                response.close()
                raise MusicProviderError(
                    "Stability returned an oversized submission response.",
                    code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                    http_status=502,
                    retryable=False,
                    outcome_unknown=True,
                )
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=8 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_SUBMIT_RESPONSE_BYTES:
                    raise MusicProviderError(
                        "Stability returned an oversized submission response.",
                        code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                        http_status=502,
                        retryable=False,
                        outcome_unknown=True,
                    )
                chunks.append(chunk)
                context.checkpoint()
        finally:
            response.close()
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("submission response must be an object")
        return payload

    @staticmethod
    def _poll_metadata(
        provider_metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        metadata = dict(provider_metadata or {})
        started_at = metadata.get("pollStartedAt")
        try:
            parsed = datetime.fromisoformat(str(started_at))
            if parsed.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            metadata["pollStartedAt"] = datetime.now(timezone.utc).isoformat()
        return metadata

    def _poll_deadline_exceeded(self, metadata: Mapping[str, Any]) -> bool:
        started_at = datetime.fromisoformat(str(metadata["pollStartedAt"]))
        elapsed = datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)
        return elapsed.total_seconds() >= self.max_poll_seconds

    def _read_audio(
        self,
        response: requests.Response,
        context: ExecutionContextProtocol,
    ) -> bytes:
        raw_length = response.headers.get("Content-Length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = 0
            if content_length > self.max_output_bytes:
                response.close()
                raise MusicProviderError(
                    "Stability audio exceeds the configured byte limit.",
                    code="MUSIC_OUTPUT_TOO_LARGE",
                    http_status=502,
                    retryable=False,
                    cost_incurred=True,
                )
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.max_output_bytes:
                    raise MusicProviderError(
                        "Stability audio exceeds the configured byte limit.",
                        code="MUSIC_OUTPUT_TOO_LARGE",
                        http_status=502,
                        retryable=False,
                        cost_incurred=True,
                    )
                chunks.append(chunk)
                context.checkpoint()
        finally:
            response.close()
        return b"".join(chunks)

    @staticmethod
    def _raise_http_error(response: requests.Response) -> None:
        status_code = int(response.status_code)
        if status_code == 429:
            code = "MUSIC_PROVIDER_RATE_LIMITED"
            retryable = True
        elif status_code in {400, 403, 404, 422}:
            code = "MUSIC_PROVIDER_REJECTED"
            retryable = False
        else:
            code = "MUSIC_PROVIDER_UNAVAILABLE"
            retryable = status_code >= 500
        raise MusicProviderError(
            "Stability rejected the music request.",
            code=code,
            http_status=503 if retryable else 400,
            retryable=retryable,
            outcome_unknown=False,
        )
