"""Bounded decode and canonical re-encode for project poster data URLs."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from typing import Optional

from django.core.files.base import ContentFile

from w_craft_back.storage_gateway import (
    StorageGatewayError,
    normalize_image_bytes,
)


MAX_PROJECT_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PROJECT_IMAGE_PIXELS = 20_000_000
_MAX_ENCODED_BYTES = 4 * ((MAX_PROJECT_IMAGE_BYTES + 2) // 3)
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/(?:jpeg|png|webp));base64,(?P<data>.+)$",
    re.IGNORECASE | re.DOTALL,
)


def decode_project_image_data_url(
    data_url,
    *,
    owner_id,
    title: str,
) -> Optional[ContentFile]:
    """Decode, verify magic/decode, and return metadata-free image bytes."""

    del owner_id, title
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
        normalized = normalize_image_bytes(
            raw,
            max_bytes=MAX_PROJECT_IMAGE_BYTES,
            max_pixels=MAX_PROJECT_IMAGE_PIXELS,
        )
    except (binascii.Error, ValueError, StorageGatewayError):
        return None
    if normalized.mime_type != match.group("mime").lower():
        return None
    return ContentFile(
        normalized.data,
        name=f"{uuid.uuid4().hex}.{normalized.extension}",
    )
