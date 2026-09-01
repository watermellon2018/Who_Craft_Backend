"""Stable public errors for the Storyboard API."""

from __future__ import annotations

from typing import Any


class StoryboardError(RuntimeError):
    """Domain error whose public shape is safe to return to clients."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "STORYBOARD_ERROR",
        http_status: int = 400,
        retryable: bool = False,
        errors: Any = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.errors = errors
        self.upstream_status = upstream_status


def validation_error(errors: Any) -> StoryboardError:
    return StoryboardError(
        "Storyboard data is invalid.",
        code="STORYBOARD_VALIDATION_ERROR",
        http_status=400,
        errors=errors,
    )


class StoryboardNotFound(StoryboardError):
    def __init__(self, detail: str = "Storyboard resource not found.") -> None:
        super().__init__(
            detail,
            code="STORYBOARD_NOT_FOUND",
            http_status=404,
        )


class StoryboardConflict(StoryboardError):
    def __init__(
        self,
        detail: str,
        *,
        code: str = "STORYBOARD_CONFLICT",
        retryable: bool = False,
    ) -> None:
        super().__init__(
            detail,
            code=code,
            http_status=409,
            retryable=retryable,
        )
