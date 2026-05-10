"""Prompt assembly for poster generation.

Pulled out of the view so the same builder is reachable from both the legacy
``/api/generate/poster/`` endpoint and the new project-scoped flow without
copy-pasting strings.
"""

from __future__ import annotations

from typing import Optional


ALLOWED_STYLES: dict[str, str] = {
    "cinematic": (
        "cinematic movie poster, dramatic composition, professional lighting, "
        "high detail, premium film poster"
    ),
    "anime": (
        "anime movie poster style, expressive character design, clean line art, "
        "dynamic composition, vibrant colors"
    ),
    "dark_fantasy": (
        "dark fantasy movie poster, gothic atmosphere, mysterious lighting, "
        "dramatic contrast, epic fantasy composition"
    ),
    "realism": (
        "photorealistic movie poster, realistic lighting, detailed environment, "
        "cinematic camera, natural textures"
    ),
}


ALLOWED_FORMATS: dict[str, str] = {
    "vertical": (
        "vertical poster composition, 2:3 aspect ratio, "
        "suitable for a movie poster cover"
    ),
    "square": (
        "square poster composition, 1:1 aspect ratio, balanced central composition"
    ),
    "horizontal": (
        "horizontal widescreen poster composition, 16:9 aspect ratio, "
        "cinematic banner layout"
    ),
}


DEFAULT_STYLE = "cinematic"
DEFAULT_FORMAT = "vertical"

PROMPT_MAX_DESCRIPTION_LENGTH = 1000


_REQUIREMENTS = (
    "No text, no logos, no watermark, no distorted anatomy, "
    "high detail, clean composition."
)


def normalize_style(value: Optional[str]) -> str:
    """Coerce ``value`` into one of ALLOWED_STYLES; fall back to default."""
    if value and value in ALLOWED_STYLES:
        return value
    return DEFAULT_STYLE


def normalize_format(value: Optional[str]) -> str:
    if value and value in ALLOWED_FORMATS:
        return value
    return DEFAULT_FORMAT


def build_poster_prompt(
    description: str,
    style: Optional[str] = None,
    format: Optional[str] = None,
    *,
    reference_present: bool = False,
) -> str:
    """Assemble the final prompt string sent to the image provider.

    The previous implementation produced lines like
    ``$Generate a movie poster. Description:Description: ...`` — the spurious
    ``$`` came from f-string templating and ``Description:`` was duplicated
    because the caller already wrapped the user text. This function owns
    formatting end-to-end so callers just hand over raw strings.
    """
    desc = (description or "").strip()
    style_key = normalize_style(style)
    format_key = normalize_format(format)

    style_text = ALLOWED_STYLES[style_key]
    format_text = ALLOWED_FORMATS[format_key]

    sections = [
        "Generate a high-quality movie poster.",
        "",
        "User description:",
        desc if desc else "(no description provided)",
        "",
        "Style:",
        f"{style_text}.",
        "",
        "Composition:",
        f"{format_text}.",
        "",
        "Requirements:",
        _REQUIREMENTS,
    ]

    if reference_present:
        sections.extend([
            "",
            "Reference:",
            (
                "A reference image was provided by the user. "
                "Match overall mood and palette where applicable."
            ),
        ])

    return "\n".join(sections)
