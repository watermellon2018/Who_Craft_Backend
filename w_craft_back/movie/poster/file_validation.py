"""Reference image upload validation.

Centralized so all poster endpoints (and any future asset endpoints that
accept the same kinds of files) get identical accept rules.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional


# Browsers report JPGs as ``image/jpeg`` reliably, but a few older clients
# and OS configurations send the unofficial ``image/jpg`` variant. Accept
# both so the FE doesn't have to special-case extensions.
ALLOWED_IMAGE_MIME_TYPES: frozenset[str] = frozenset({
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
})

ALLOWED_IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
})

REFERENCE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


class ReferenceImageValidationError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_REFERENCE_IMAGE"):
        super().__init__(message)
        self.code = code
        self.message = message


def _ext_of(filename: Optional[str]) -> str:
    if not filename:
        return ""
    return os.path.splitext(filename.lower())[1]


def validate_reference_image(uploaded_file) -> None:
    """Raise ``ReferenceImageValidationError`` if the file is not acceptable.

    Validates by extension first (most reliable on the wire) and only
    rejects on MIME type when the client *did* set one and it disagrees —
    a missing/empty content_type is tolerated, since some legacy clients
    forget the header.
    """
    if uploaded_file is None:
        return

    name = getattr(uploaded_file, "name", "") or ""
    ext = _ext_of(name)
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ReferenceImageValidationError(
            "Поддерживаются только PNG, JPG, JPEG или WEBP.",
            code="INVALID_REFERENCE_EXTENSION",
        )

    content_type = (getattr(uploaded_file, "content_type", "") or "").lower()
    if content_type and content_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise ReferenceImageValidationError(
            "Неподдерживаемый тип изображения.",
            code="INVALID_REFERENCE_MIME",
        )

    size = getattr(uploaded_file, "size", None)
    if size is not None and size > REFERENCE_MAX_BYTES:
        raise ReferenceImageValidationError(
            "Максимальный размер изображения — 10 MB.",
            code="REFERENCE_FILE_TOO_LARGE",
        )


def is_acceptable_image_filename(filename: str) -> bool:
    return _ext_of(filename) in ALLOWED_IMAGE_EXTENSIONS
