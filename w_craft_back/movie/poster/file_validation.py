"""Canonical reference-image validation shared by all poster endpoints."""

from __future__ import annotations

from w_craft_back.storage_gateway import (
    InvalidImage,
    MediaTooLarge,
    NormalizedImage,
    StorageGatewayError,
    UnsupportedMedia,
    normalize_image_upload,
)


ALLOWED_IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp"}
)
REFERENCE_MAX_BYTES = 10 * 1024 * 1024


class ReferenceImageValidationError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_REFERENCE_IMAGE"):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_reference_image(uploaded_file) -> NormalizedImage | None:
    """Decode and canonicalize an image; never trust its name or MIME header."""

    if uploaded_file is None:
        return None
    try:
        normalized = normalize_image_upload(
            uploaded_file,
            max_bytes=REFERENCE_MAX_BYTES,
        )
    except MediaTooLarge as exc:
        raise ReferenceImageValidationError(
            "Максимальный размер изображения — 10 MB.",
            code="REFERENCE_FILE_TOO_LARGE",
        ) from exc
    except (InvalidImage, UnsupportedMedia, StorageGatewayError) as exc:
        raise ReferenceImageValidationError(
            "Файл не является допустимым PNG, JPEG или WEBP изображением.",
            code="INVALID_REFERENCE_IMAGE",
        ) from exc
    uploaded_file._storage_gateway_normalized = normalized
    return normalized
