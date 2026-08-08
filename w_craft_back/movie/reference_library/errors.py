"""Stable domain errors for the Reference Library API."""

from __future__ import annotations

from typing import Any

from w_craft_back.services.image_generation.errors import ImageProviderError
from w_craft_back.storage_gateway import StorageGatewayError


class ReferenceError(Exception):
    """Base error serialized by reference-library views."""

    code = "REFERENCE_ERROR"
    http_status = 400
    retryable = False

    def __init__(
        self,
        detail: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
        errors: dict[str, Any] | None = None,
        current_version: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code or self.code
        self.http_status = http_status or self.http_status
        self.retryable = self.retryable if retryable is None else retryable
        self.errors = errors
        self.current_version = current_version


class ReferenceNotFound(ReferenceError):
    code = "REFERENCE_NOT_FOUND"
    http_status = 404


class ReferenceVersionNotFound(ReferenceError):
    code = "REFERENCE_VERSION_NOT_FOUND"
    http_status = 404


class ReferenceJobNotFound(ReferenceError):
    code = "REFERENCE_JOB_NOT_FOUND"
    http_status = 404


class ReferenceVariantNotFound(ReferenceError):
    code = "REFERENCE_VARIANT_NOT_FOUND"
    http_status = 404


class ReferencePermissionDenied(ReferenceError):
    http_status = 403


class ReferenceConflict(ReferenceError):
    http_status = 409


def validation_error(
    errors: dict[str, Any],
    detail: str = "Invalid request.",
) -> ReferenceError:
    """Build the stable validation envelope used by all write endpoints."""

    return ReferenceError(
        detail,
        code="REFERENCE_INVALID_BRIEF",
        errors=errors,
    )


def map_storage_error(error: StorageGatewayError) -> ReferenceError:
    """Translate the shared storage validation contract."""

    return ReferenceError(
        error.message,
        code=error.code,
        http_status=error.http_status,
    )


def map_provider_error(error: ImageProviderError) -> ReferenceError:
    """Translate image-provider failures without exposing SDK details."""

    return ReferenceError(
        str(getattr(error, "message", "Image provider failed.")),
        code=str(getattr(error, "code", "IMAGE_PROVIDER_UNAVAILABLE")),
        http_status=int(getattr(error, "http_status", 503)),
        retryable=int(getattr(error, "http_status", 503)) >= 500,
    )
