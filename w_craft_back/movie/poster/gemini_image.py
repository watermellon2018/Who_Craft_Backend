"""Thin Gemini/Imagen client for the poster generator.

Mirrors the working pattern from ``character_studio.services.providers``:
REST POST to the Generative Language API with ``x-goog-api-key`` and an
``instances``/``parameters`` body.

Returns decoded PNG bytes for the unified image-provider adapter.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, Optional

import requests

from w_craft_back.movie.poster.generation_guard import max_output_bytes

logger = logging.getLogger(__name__)


# Map the FE format key to Gemini's accepted aspectRatio values.
ASPECT_BY_FORMAT: dict[str, str] = {
    "vertical": "3:4",
    "square": "1:1",
    "horizontal": "16:9",
}


def _config() -> dict[str, str]:
    return {
        "api_key": os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "",
        "endpoint_base": os.getenv(
            "GEMINI_API_BASE_URL",
            "https://generativelanguage.googleapis.com",
        ),
        "model": os.getenv(
            "GEMINI_POSTER_MODEL",
            os.getenv("GEMINI_IMAGE_MODEL", "imagen-4.0-generate-001"),
        ),
        "edit_model": os.getenv(
            "GEMINI_POSTER_EDIT_MODEL",
            "gemini-2.5-flash-image",
        ),
        "person_generation": os.getenv("GEMINI_PERSON_GENERATION", "allow_adult"),
        "safety_filter": os.getenv("GEMINI_SAFETY_FILTER_LEVEL", "block_few"),
    }


def _aspect_for(format_key: Optional[str]) -> str:
    return ASPECT_BY_FORMAT.get((format_key or "").lower(), "3:4")


def _decode_provider_image(encoded: Any) -> bytes:
    """Decode one provider image while bounding encoded and decoded payloads."""
    if not isinstance(encoded, str) or not encoded:
        raise GeminiImageError(
            "Gemini response is missing image bytes",
            kind="bad_response",
        )

    output_limit = max_output_bytes()
    max_encoded_size = ((output_limit + 2) // 3) * 4
    if len(encoded) > max_encoded_size:
        raise GeminiImageError(
            "Gemini returned an image larger than the configured output limit",
            kind="bad_response",
        )

    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise GeminiImageError(
            "Gemini returned invalid base64 image",
            kind="bad_response",
        ) from exc

    if len(decoded) > output_limit:
        raise GeminiImageError(
            "Gemini returned an image larger than the configured output limit",
            kind="bad_response",
        )
    return decoded


def generate_image_via_gemini(
    prompt: str,
    *,
    poster_format: Optional[str] = None,
    timeout_seconds: float = 120,
) -> bytes:
    """Call Gemini/Imagen and return PNG bytes for one generated image.

    Raises ``GeminiImageError`` on any provider failure; the caller in
    ``views/views.py`` translates that into the project's ``ImageProviderError``
    so the existing JSON response contract is preserved.
    """
    cfg = _config()
    if not cfg["api_key"]:
        raise GeminiImageError(
            "GEMINI_API_KEY is not set",
            kind="not_configured",
        )

    url = f"{cfg['endpoint_base']}/v1beta/models/{cfg['model']}:predict"
    headers = {
        "x-goog-api-key": cfg["api_key"],
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": _aspect_for(poster_format),
            "personGeneration": cfg["person_generation"],
            "safetyFilterLevel": cfg["safety_filter"],
        },
    }

    session = requests.Session()
    # Same defensive setting as in character_studio: ignore broken local proxy
    # env vars in Windows/dev environments.
    session.trust_env = False

    try:
        response = session.post(
            url, headers=headers, json=payload, timeout=timeout_seconds
        )
    except requests.RequestException as exc:
        logger.error(
            "gemini_transport_error",
            extra={"exception_type": type(exc).__name__},
        )
        raise GeminiImageError(
            "Gemini provider unavailable",
            kind="unavailable",
        ) from exc

    if response.status_code in (401, 403):
        logger.error("Gemini rejected request: status=%s", response.status_code)
        raise GeminiImageError(
            "Gemini rejected the request (check GEMINI_API_KEY and project access).",
            kind="forbidden",
            provider_status=response.status_code,
        )

    if response.status_code >= 400:
        logger.error("Gemini error: status=%s", response.status_code)
        raise GeminiImageError(
            f"Gemini returned HTTP {response.status_code}",
            kind="error",
            provider_status=response.status_code,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise GeminiImageError(
            "Gemini returned a non-JSON response",
            kind="bad_response",
            provider_status=response.status_code,
        ) from exc

    block_reason = (data.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        raise GeminiImageError(
            f"Gemini blocked the prompt: {block_reason}",
            kind="blocked",
            provider_body=str(block_reason),
        )

    predictions = data.get("predictions") or []
    if not predictions:
        logger.warning("Gemini returned no predictions")
        raise GeminiImageError(
            "Gemini returned no predictions (likely a safety filter hit).",
            kind="empty",
        )

    pred = predictions[0]
    b64 = (
        pred.get("bytesBase64Encoded")
        or (pred.get("image") or {}).get("bytesBase64Encoded")
        or pred.get("imageBytes")
    )
    if not b64:
        raise GeminiImageError(
            "Gemini prediction is missing image bytes",
            kind="bad_response",
        )

    return _decode_provider_image(b64)


def edit_image_via_gemini(
    image_bytes: bytes,
    instruction: str,
    *,
    mime_type: str = "image/png",
    timeout_seconds: float = 120,
) -> bytes:
    """Edit ``image_bytes`` according to ``instruction`` and return new PNG bytes.

    Imagen's ``:predict`` endpoint is text-to-image only. For image-in /
    image-out we use the Gemini multimodal model
    (``gemini-2.5-flash-image-preview`` aka "Nano Banana"), which accepts an
    inline image plus a text instruction via ``:generateContent`` and returns
    the edited image as ``inlineData`` parts on the response.
    """
    cfg = _config()
    if not cfg["api_key"]:
        raise GeminiImageError(
            "GEMINI_API_KEY is not set",
            kind="not_configured",
        )

    model = cfg["edit_model"]
    url = f"{cfg['endpoint_base']}/v1beta/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": cfg["api_key"],
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": mime_type or "image/png",
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                    {"text": instruction},
                ],
            }
        ],
        # ``responseModalities`` is required for the image preview model so it
        # actually emits image bytes, not just descriptive text.
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
        },
    }

    session = requests.Session()
    session.trust_env = False

    try:
        response = session.post(
            url, headers=headers, json=payload, timeout=timeout_seconds
        )
    except requests.RequestException as exc:
        logger.error(
            "gemini_edit_transport_error",
            extra={"exception_type": type(exc).__name__},
        )
        raise GeminiImageError(
            "Gemini provider unavailable",
            kind="unavailable",
        ) from exc

    if response.status_code in (401, 403):
        logger.error("Gemini edit rejected: status=%s", response.status_code)
        raise GeminiImageError(
            "Gemini rejected the request (check GEMINI_API_KEY and project access).",
            kind="forbidden",
            provider_status=response.status_code,
        )

    if response.status_code >= 400:
        logger.error("Gemini edit error: status=%s", response.status_code)
        raise GeminiImageError(
            f"Gemini returned HTTP {response.status_code}",
            kind="error",
            provider_status=response.status_code,
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise GeminiImageError(
            "Gemini returned a non-JSON response",
            kind="bad_response",
            provider_status=response.status_code,
        ) from exc

    block_reason = (data.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        raise GeminiImageError(
            f"Gemini blocked the prompt: {block_reason}",
            kind="blocked",
            provider_body=str(block_reason),
        )

    candidates = data.get("candidates") or []
    for cand in candidates:
        parts = ((cand or {}).get("content") or {}).get("parts") or []
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return _decode_provider_image(inline["data"])

    # No inline image in any candidate — likely a safety filter or the model
    # only returned text describing what it would do.
    logger.warning("Gemini edit returned no image parts")
    raise GeminiImageError(
        "Gemini did not return an edited image (likely a safety filter hit).",
        kind="empty",
    )


class GeminiImageError(Exception):
    """Internal Gemini failure; views map this to ``ImageProviderError``."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        provider_status: int | None = None,
        provider_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.provider_status = provider_status
        self.provider_body = provider_body
