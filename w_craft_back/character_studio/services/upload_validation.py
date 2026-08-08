"""Decode-based validation for Character Studio image uploads."""

from __future__ import annotations

from typing import Tuple

from w_craft_back.storage_gateway import (
    MediaTooLarge,
    StorageGatewayError,
    normalize_image_upload,
)


MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class UploadValidationError(Exception):
    def __init__(self, error_code: str, message: str, status: int = 400):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status = status


def validate_image_upload(uploaded) -> Tuple[str, str]:
    """Decode/re-encode the upload and return trusted MIME/extension."""

    if not uploaded:
        raise UploadValidationError("NO_FILE", "No file provided.")
    try:
        normalized = normalize_image_upload(
            uploaded,
            max_bytes=MAX_UPLOAD_BYTES,
        )
    except MediaTooLarge as exc:
        raise UploadValidationError(
            "FILE_TOO_LARGE",
            "File exceeds 10 MB limit.",
            status=413,
        ) from exc
    except StorageGatewayError as exc:
        raise UploadValidationError(
            "INVALID_FORMAT",
            "File is not a valid jpg, png or webp image.",
            status=415,
        ) from exc
    uploaded._storage_gateway_normalized = normalized
    return normalized.mime_type, normalized.extension
