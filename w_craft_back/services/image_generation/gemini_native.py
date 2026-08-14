"""Thin :class:`ImageProvider` wrapper around the existing native Gemini REST
client. Kept as a safety net: if LiteLLM routing breaks, the user can switch
to the ``"gemini-native"`` key and keep working.
"""

from __future__ import annotations

from typing import Any
from decimal import Decimal

from .errors import map_to_provider_error
from .registry import ModelSpec
from .usage import merge_usage, normalized_response_usage


_ASPECT_BY_FORMAT_KEY = {
    "vertical": "vertical",
    "square": "square",
    "horizontal": "horizontal",
}

_FORMAT_BY_ASPECT_RATIO = {
    "3:4": "vertical",
    "1:1": "square",
    "16:9": "horizontal",
}


def _format_from_aspect(aspect_ratio: str | None) -> str | None:
    if not aspect_ratio:
        return None
    key = aspect_ratio.strip()
    if key in _ASPECT_BY_FORMAT_KEY:
        return _ASPECT_BY_FORMAT_KEY[key]
    return _FORMAT_BY_ASPECT_RATIO.get(key)


class GeminiNativeProvider:
    name = "gemini-native"

    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec
        self.model_id = spec.model_id
        self._usage_events: list[dict[str, Any]] = []

    def usage_snapshot(self) -> dict[str, Any]:
        return merge_usage(self._usage_events)

    def _record_usage(self, usage: dict[str, Any], *, operation: str) -> None:
        event = normalized_response_usage({"usage": usage})
        pricing = self.spec.provider_pricing or {}
        output = pricing.get(f"{operation}_output_image") or pricing.get("output_image")
        input_rate = pricing.get(f"{operation}_input_text_token")
        if output is not None:
            cost = Decimal(str(output))
            prompt_tokens = event.get("promptTokens", 0)
            if input_rate is not None and isinstance(prompt_tokens, int):
                cost += Decimal(str(input_rate)) * prompt_tokens
            event["costUsd"] = format(cost, "f")
            event["costSource"] = "provider-rate"
        self._usage_events.append(event)

    def supports_edit(self) -> bool:
        return self.spec.supports_edit

    def supports_reference(self) -> bool:
        return self.spec.supports_reference

    def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str | None = None,
        variant_count: int = 1,
        **kwargs: Any,
    ) -> list[bytes]:
        # Lazy import keeps this module decoupled from the legacy REST client
        # for test isolation.
        from w_craft_back.movie.poster.gemini_image import generate_image_via_gemini

        poster_format = _format_from_aspect(aspect_ratio)
        results: list[bytes] = []
        try:
            # Native client returns one image per call; loop for multi-variant.
            for _ in range(max(1, int(variant_count or 1))):
                image, usage = generate_image_via_gemini(
                        prompt,
                        poster_format=poster_format,
                        timeout_seconds=kwargs.get("timeout", 120),
                        return_usage=True,
                    )
                results.append(image)
                self._record_usage(usage, operation="generate")
        except Exception as exc:  # noqa: BLE001 — funnel through unified mapper
            raise map_to_provider_error(exc) from exc
        return results

    def edit(
        self,
        image_bytes: bytes,
        instruction: str,
        *,
        mime_type: str = "image/png",
        **kwargs: Any,
    ) -> bytes:
        from w_craft_back.movie.poster.gemini_image import edit_image_via_gemini

        try:
            image, usage = edit_image_via_gemini(
                image_bytes,
                instruction,
                mime_type=mime_type,
                timeout_seconds=kwargs.get("timeout", 120),
                return_usage=True,
            )
            self._record_usage(usage, operation="edit")
            return image
        except Exception as exc:  # noqa: BLE001
            raise map_to_provider_error(exc) from exc
