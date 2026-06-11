"""Image upload validation: MIME + magic-byte signature whitelist.

Clients control the ``Content-Type`` header, so we must independently sniff
the first bytes of the upload to refuse e.g. an HTML file masquerading as
``image/jpeg``. Kept as a module instead of using ``python-magic`` to avoid
a libmagic system dependency on Windows dev boxes.
"""
from __future__ import annotations

from typing import Optional, Tuple

from w_craft_back.character_studio.services.asset_service import (
    ALLOWED_UPLOAD_MIME,
    MAX_UPLOAD_BYTES,
    MIME_TO_EXT,
)


_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    # WebP: "RIFF....WEBP". Detect both anchors with a custom check below.
)


def _sniff_mime(head: bytes) -> Optional[str]:
    for sig, mime in _SIGNATURES:
        if head.startswith(sig):
            return mime
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class UploadValidationError(Exception):
    def __init__(self, error_code: str, message: str, status: int = 400):
        self.error_code = error_code
        self.message = message
        self.status = status


def validate_image_upload(uploaded) -> Tuple[str, str]:
    """Return ``(mime, extension)`` for an UploadedFile or raise UploadValidationError.

    Validates: presence, declared MIME, size, sniffed magic bytes vs declared MIME.
    """
    if not uploaded:
        raise UploadValidationError("NO_FILE", "No file provided.")
    declared = (uploaded.content_type or "").lower()
    if declared not in ALLOWED_UPLOAD_MIME:
        raise UploadValidationError("INVALID_FORMAT", "Only jpg, png and webp are supported.")
    if uploaded.size > MAX_UPLOAD_BYTES:
        raise UploadValidationError("FILE_TOO_LARGE", "File exceeds 10 MB limit.")

    head = uploaded.read(16)
    # Rewind so subsequent .chunks() reads start from byte 0.
    try:
        uploaded.seek(0)
    except (AttributeError, OSError):
        # In-memory uploads always support seek; chunked temp uploads do too.
        # Fall through; if seek fails we still report a clear error.
        raise UploadValidationError("INVALID_FORMAT", "Uploaded file is not seekable.")

    sniffed = _sniff_mime(head or b"")
    if sniffed is None or sniffed != declared:
        raise UploadValidationError(
            "INVALID_FORMAT",
            "File contents do not match declared image type.",
        )

    return declared, MIME_TO_EXT[declared]
