"""Shared bounded prompt helpers for commercial music adapters."""

from __future__ import annotations

import math
from typing import Any, Mapping
from urllib.parse import urlsplit


def origin_allowed(
    value: str,
    *,
    official_hostname: str,
    explicit: bool,
) -> bool:
    """Allow only the official HTTPS origin outside explicit test injection."""

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
    return parsed.hostname == official_hostname and not parsed.path


def musical_direction(request: Mapping[str, Any], *, maximum: int) -> str:
    """Return a bounded positive direction with explicit exclusions."""

    positive = str(request.get("positivePrompt") or "").strip()
    if not positive:
        raise ValueError("A musical direction is required.")
    negative = str(request.get("negativePrompt") or "").strip()
    suffix = f". Avoid: {negative[:1000]}" if negative else ""
    return f"{positive}{suffix}"[:maximum]


def formatted_lyrics(
    request: Mapping[str, Any],
    *,
    maximum: int,
) -> str:
    """Preserve ordered user lyrics in common provider section syntax."""

    sections = request.get("lyricsSections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("Songs require ordered lyrics sections.")
    parts: list[str] = []
    for section in sections:
        if not isinstance(section, Mapping):
            raise ValueError("Lyrics sections must be objects.")
        section_type = str(section.get("type") or "").strip()
        text = str(section.get("text") or "").strip()
        if not section_type or not text:
            raise ValueError("Lyrics sections require a type and text.")
        heading = section_type.title()
        parts.append(f"[{heading}]\n{text}")
    lyrics = "\n\n".join(parts)
    if len(lyrics) > maximum:
        raise ValueError("Lyrics exceed the provider limit.")
    return lyrics


def elevenlabs_composition_plan(
    request: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Build a valid Music v2 plan while preserving every lyric section."""

    duration_seconds = int(request["durationSeconds"])
    sections = request.get("lyricsSections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("Songs require ordered lyrics sections.")
    maximum_chunks_by_duration = max(1, duration_seconds // 3)
    minimum_chunks_by_duration = max(1, math.ceil(duration_seconds / 120))
    chunk_count = max(
        minimum_chunks_by_duration,
        min(len(sections), maximum_chunks_by_duration),
    )
    if chunk_count > 30:
        raise ValueError("The composition plan exceeds the provider limit.")

    groups: list[list[Mapping[str, Any]]] = [
        [] for _ in range(chunk_count)
    ]
    for index, section in enumerate(sections):
        if not isinstance(section, Mapping):
            raise ValueError("Lyrics sections must be objects.")
        group_index = min(index * chunk_count // len(sections), chunk_count - 1)
        groups[group_index].append(section)

    total_ms = duration_seconds * 1000
    base_duration = total_ms // chunk_count
    remainder = total_ms % chunk_count
    direction = musical_direction(request, maximum=2000)
    negative = str(request.get("negativePrompt") or "").strip()
    chunks: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        duration_ms = base_duration + (1 if index < remainder else 0)
        if not 3000 <= duration_ms <= 120_000:
            raise ValueError("A composition chunk has an invalid duration.")
        if group:
            group_request = {"lyricsSections": list(group)}
            text = formatted_lyrics(group_request, maximum=12_000)
        else:
            text = "[Instrumental]"
        chunks.append(
            {
                "text": text,
                "duration_ms": duration_ms,
                "positive_styles": [direction],
                "negative_styles": [negative[:1000]] if negative else [],
                "context_adherence": "high",
            }
        )
    return {"chunks": chunks}
