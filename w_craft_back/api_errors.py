"""Uniform error envelopes for every JSON response under ``/api/``."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from django.http import JsonResponse


_KNOWN_ERROR_KEYS = {
    "code",
    "detail",
    "error",
    "error_code",
    "errors",
    "message",
}


def _default_code(status_code: int) -> str:
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        phrase = "API error"
    return re.sub(r"[^A-Z0-9]+", "_", phrase.upper()).strip("_") or "API_ERROR"


def _first_message(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (list, tuple)) and value:
        return _first_message(value[0])
    if isinstance(value, Mapping):
        for nested_value in value.values():
            message = _first_message(nested_value)
            if message:
                return message
    return None


def normalize_error_payload(payload: Any, status_code: int) -> dict[str, Any]:
    """Add the canonical envelope while retaining temporary legacy aliases."""

    original = dict(payload) if isinstance(payload, Mapping) else {}
    existing_error = original.get("error")
    existing_envelope = existing_error if isinstance(existing_error, Mapping) else {}

    code = str(
        existing_envelope.get("code")
        or original.get("code")
        or original.get("error_code")
        or _default_code(status_code)
    )
    fields = existing_envelope.get("fields") or original.get("errors")
    if fields is None and original and not (_KNOWN_ERROR_KEYS & original.keys()):
        fields = original
    message = (
        _first_message(existing_envelope.get("message"))
        or _first_message(fields)
        or _first_message(original.get("detail"))
        or _first_message(original.get("message"))
        or _first_message(existing_error)
        or _default_code(status_code).replace("_", " ").title()
    )

    error: dict[str, Any] = {"code": code, "message": message}
    if fields:
        error["fields"] = fields

    # ``code/detail/errors/error_code`` remain during the frontend migration.
    # New code consumes only ``error``.
    normalized = dict(original)
    normalized["error"] = error
    normalized.setdefault("code", code)
    normalized.setdefault("detail", message)
    if fields is not None:
        normalized.setdefault("errors", fields)
    return normalized


class ApiErrorEnvelopeMiddleware:
    """Normalize JSON API errors without changing their HTTP status."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith("/api/") or response.status_code < 400:
            return response
        content_type = response.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json" or getattr(response, "streaming", False):
            return response
        try:
            payload = json.loads(response.content.decode(response.charset or "utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return response

        normalized = normalize_error_payload(payload, response.status_code)
        replacement = JsonResponse(normalized, status=response.status_code)
        for header, value in response.items():
            if header.lower() not in {"content-length", "content-type"}:
                replacement[header] = value
        return replacement


def api_error_response(
    *,
    code: str,
    message: str,
    status: int,
    fields: Mapping[str, Any] | None = None,
) -> JsonResponse:
    """Build an error response already conforming to the canonical envelope."""

    payload: dict[str, Any] = {
        "error": {"code": code, "message": message},
        "code": code,
        "detail": message,
    }
    if fields:
        payload["error"]["fields"] = dict(fields)
        payload["errors"] = dict(fields)
    return JsonResponse(payload, status=status)
