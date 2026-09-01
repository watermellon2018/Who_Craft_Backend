"""Shared prompt and response helpers for Google Lyria adapters."""

from __future__ import annotations

import base64
import binascii
from typing import Any, Mapping


_MAX_DIRECTION_CHARS = 10_000
_MAX_NEGATIVE_CHARS = 1_000
_MAX_LYRICS_CHARS = 12_000
_MAX_TRANSCRIPT_CHARS = 1_000
_MODEL_LIMITS = {
    "lyria-3-pro-preview": (3, 180),
    "lyria-3-clip-preview": (30, 30),
}


def validate_lyria_model(model_name: str) -> str:
    """Return a supported upstream model name or raise ``ValueError``."""

    normalized = str(model_name or "").strip()
    if normalized not in _MODEL_LIMITS:
        raise ValueError("Unsupported Google Lyria model.")
    return normalized


def build_lyria_prompt(
    request: Mapping[str, Any],
    *,
    model_name: str,
) -> str:
    """Compile the provider-neutral request into a bounded Lyria prompt."""

    minimum, maximum = _MODEL_LIMITS[validate_lyria_model(model_name)]
    raw_duration = request.get("durationSeconds")
    if isinstance(raw_duration, bool):
        raise ValueError("Lyria duration must be an integer.")
    try:
        duration = int(raw_duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("Lyria duration must be an integer.") from exc
    if not minimum <= duration <= maximum:
        raise ValueError("Lyria duration is outside the model limits.")

    mode = str(request.get("contentMode") or "").strip()
    if mode not in {"instrumental", "song"}:
        raise ValueError("Lyria content mode must be instrumental or song.")
    direction = str(request.get("positivePrompt") or "").strip()
    if not direction:
        raise ValueError("Lyria requires a musical direction.")

    lines = [
        f"Create a {duration}-second piece of music.",
        f"Musical direction: {direction[:_MAX_DIRECTION_CHARS]}",
    ]
    negative = str(request.get("negativePrompt") or "").strip()
    if negative:
        lines.append(f"Avoid: {negative[:_MAX_NEGATIVE_CHARS]}")

    if mode == "instrumental":
        lines.append(
            "Instrumental only. Do not include vocals, speech, chanting, or lyrics."
        )
        return "\n".join(lines)

    language = str(request.get("lyricsLanguage") or "").strip()
    if language not in {"ru", "en"}:
        raise ValueError("Lyria song lyrics must use Russian or English.")
    language_name = "Russian" if language == "ru" else "English"
    lines.append(f"Lyrics language: {language_name}.")

    vocal_style = request.get("vocalStyle") or {}
    if not isinstance(vocal_style, Mapping):
        raise ValueError("Lyria vocal style must be an object.")
    vocal_parts = [
        f"{key}: {str(vocal_style[key]).strip()[:200]}"
        for key in ("timbre", "delivery", "density")
        if str(vocal_style.get(key) or "").strip()
    ]
    if vocal_parts:
        lines.append(f"Vocal style: {'; '.join(vocal_parts)}.")

    raw_sections = request.get("lyricsSections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ValueError("Lyria songs require ordered lyrics sections.")
    lyrics_parts: list[str] = []
    lyrics_chars = 0
    for raw_section in raw_sections:
        if not isinstance(raw_section, Mapping):
            raise ValueError("Lyria lyrics sections must be objects.")
        section_type = str(raw_section.get("type") or "").strip()
        text = str(raw_section.get("text") or "").strip()
        if not section_type or not text:
            raise ValueError("Lyria lyrics sections require a type and text.")
        label = str(raw_section.get("label") or "").strip()
        remaining = _MAX_LYRICS_CHARS - lyrics_chars
        if remaining <= 0:
            raise ValueError("Lyria lyrics exceed the supported length.")
        bounded_text = text[:remaining]
        lyrics_chars += len(bounded_text)
        heading = section_type.upper()
        if label:
            heading = f"{heading}: {label[:100]}"
        lyrics_parts.append(f"[{heading}]\n{bounded_text}")
    lines.extend(
        (
            "Use exactly the following lyrics and section order. "
            "Do not rewrite or add lyrics.",
            "\n\n".join(lyrics_parts),
        )
    )
    return "\n".join(lines)


def decode_bounded_audio(encoded: str, *, max_output_bytes: int) -> bytes:
    """Strictly decode base64 without allowing an oversized audio allocation."""

    compact = "".join(str(encoded or "").split())
    if not compact:
        raise ValueError("Provider response contains no audio data.")
    max_encoded = ((max_output_bytes + 2) // 3) * 4
    if len(compact) > max_encoded:
        raise OverflowError("Provider audio exceeds the configured byte limit.")
    try:
        payload = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Provider returned invalid base64 audio.") from exc
    if len(payload) > max_output_bytes:
        raise OverflowError("Provider audio exceeds the configured byte limit.")
    return payload


def transcript_summary(parts: list[str]) -> str:
    """Return a whitespace-normalized, bounded provider transcript summary."""

    return " ".join(" ".join(parts).split())[:_MAX_TRANSCRIPT_CHARS]
