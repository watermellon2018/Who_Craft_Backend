"""Stable error codes for the poster API.

Codes are part of the public API contract and stay stable across refactors
(strings, not enum values, so adding new codes never reorders anything).
The ``raise``-based control flow lets services be called from workers or
tests without importing DRF Response shapes.
"""

from __future__ import annotations

from rest_framework import status


class PosterError(Exception):
    """Service-layer error with a stable code + HTTP status.

    Views translate this into the project's canonical error response shape:
    ``{"detail": <message>, "code": <code>, "errors": <field_errors_or_None>}``.
    """

    http_status: int = status.HTTP_400_BAD_REQUEST
    code: str = "POSTER_ERROR"

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        http_status: int | None = None,
        errors: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message or self.code
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.errors = errors


class ProjectNotFound(PosterError):
    http_status = status.HTTP_404_NOT_FOUND
    code = "PROJECT_NOT_FOUND"


class ProjectAccessDenied(PosterError):
    http_status = status.HTTP_403_FORBIDDEN
    code = "PROJECT_ACCESS_DENIED"


class PromptRequired(PosterError):
    code = "POSTER_PROMPT_REQUIRED"


class PromptTooLong(PosterError):
    code = "POSTER_PROMPT_TOO_LONG"


class InvalidPosterStyle(PosterError):
    code = "INVALID_POSTER_STYLE"


class InvalidPosterFormat(PosterError):
    code = "INVALID_POSTER_FORMAT"


class PosterJobNotFound(PosterError):
    http_status = status.HTTP_404_NOT_FOUND
    code = "POSTER_JOB_NOT_FOUND"


class PosterVariantNotFound(PosterError):
    http_status = status.HTTP_404_NOT_FOUND
    code = "POSTER_VARIANT_NOT_FOUND"


class PosterVariantDeleted(PosterError):
    code = "POSTER_VARIANT_DELETED"
