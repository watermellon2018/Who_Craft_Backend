"""Safe structured logging and request correlation."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator


_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)

# Only explicitly approved metadata is serialized. In particular, prompt,
# request body, provider response and authentication values are excluded.
_STRUCTURED_FIELDS = (
    "request_id",
    "job_id",
    "project_id",
    "character_id",
    "asset_id",
    "provider",
    "model",
    "operation",
    "image_type",
    "region",
    "variant_count",
    "prompt_hash",
    "prompt_len",
    "method",
    "route",
    "status",
    "status_code",
    "duration_ms",
    "component",
    "worker_mode",
    "error_code",
    "exception_type",
)


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _request_route(request: Any) -> str:
    """Return a route template, never a user-controlled URL or query string."""

    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None)
    if route is None:
        return "<unmatched>"
    return f"/{str(route).lstrip('/')}"


def _validated_request_id(value: str | None) -> str:
    candidate = (value or "").strip()
    if candidate and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


@contextmanager
def log_context(*, job_id: Any = None) -> Iterator[None]:
    """Attach durable job correlation to every log emitted in the block."""

    token = _job_id.set(str(job_id) if job_id is not None else None)
    try:
        yield
    finally:
        _job_id.reset(token)


class JsonLogFormatter(logging.Formatter):
    """Emit a stable JSON envelope without exception text or arbitrary extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        context_values = {
            "request_id": _request_id.get(),
            "job_id": _job_id.get(),
        }
        for field in _STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is None:
                value = context_values.get(field)
            if value is not None:
                payload[field] = _safe_scalar(value)

        # Tracebacks and exception messages can contain provider payloads,
        # prompts, signed URLs or credentials. Keep the actionable type only.
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = record.exc_info[0].__name__

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class SafeDjangoRequestFilter(logging.Filter):
    """Replace Django's raw-path request log with safe structured metadata."""

    def filter(self, record: logging.LogRecord) -> bool:
        request = getattr(record, "request", None)
        status_code = getattr(record, "status_code", None)
        try:
            is_error = int(status_code) >= 400
        except (TypeError, ValueError):
            is_error = record.levelno >= logging.WARNING
        record.msg = (
            "django_request_error" if is_error else "django_request_completed"
        )
        record.args = ()
        if request is not None:
            request_id = getattr(request, "request_id", None)
            if request_id is not None:
                record.request_id = request_id
            record.method = getattr(request, "method", "")
            record.route = _request_route(request)
        return True


class RequestContextMiddleware:
    """Correlate each response and emit one safe HTTP completion event."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.logger = logging.getLogger("w_craft_back.request")

    def __call__(self, request):
        request_id = _validated_request_id(request.headers.get(_REQUEST_ID_HEADER))
        request.request_id = request_id
        token = _request_id.set(request_id)
        started_at = time.monotonic()
        try:
            response = self.get_response(request)
            response[_REQUEST_ID_HEADER] = request_id
            route = _request_route(request)
            if route.startswith("/health/"):
                log_level = (
                    logging.WARNING if response.status_code >= 500 else logging.DEBUG
                )
            else:
                log_level = logging.INFO
            self.logger.log(
                log_level,
                "http_request_completed",
                extra={
                    "method": request.method,
                    "route": route,
                    "status_code": response.status_code,
                    "duration_ms": round(
                        (time.monotonic() - started_at) * 1000,
                        2,
                    ),
                },
            )
            return response
        finally:
            _request_id.reset(token)
