"""Bounded decoding for project poster data URLs."""

from __future__ import annotations

import base64
import binascii
import io
import re
import uuid
import warnings
from typing import Optional

from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError


MAX_PROJECT_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PROJECT_IMAGE_PIXELS = 20_000_000
_MAX_ENCODED_BYTES = 4 * ((MAX_PROJECT_IMAGE_BYTES + 2) // 3)
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/(?:jpeg|png|webp));base64,(?P<data>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def decode_project_image_data_url(
    data_url,
    *,
    owner_id,
    title: str,
) -> Optional[ContentFile]:
    """Decode an allowed image data URL without unbounded base64 allocation."""
    if not isinstance(data_url, str) or not data_url:
        return None
    if len(data_url) > _MAX_ENCODED_BYTES + 64:
        return None

    match = _DATA_URL_RE.fullmatch(data_url.strip())
    if match is None:
        return None
    encoded = match.group("data")
    if len(encoded) > _MAX_ENCODED_BYTES:
        return None

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) > MAX_PROJECT_IMAGE_BYTES:
        return None

    mime = match.group("mime").lower()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                image.verify()
                actual_format = (image.format or "").upper()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
        ValueError,
    ):
        return None
    if width * height > MAX_PROJECT_IMAGE_PIXELS:
        return None
    expected_format = {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/webp": "WEBP",
    }[mime]
    if actual_format != expected_format:
        return None

    safe_title = re.sub(r"[^\w\-.]+", "_", title or "project")[:60]
    filename = (
        f"{owner_id or 'anon'}/{safe_title}/{uuid.uuid4()}."
        f"{_EXTENSIONS[mime]}"
    )
    return ContentFile(raw, name=filename)
