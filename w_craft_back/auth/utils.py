from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Optional

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from w_craft_back.auth.models import UserKey
from w_craft_back.auth.tokens import authenticate_access_token

logger = logging.getLogger(__name__)

_BODY_FALLBACK_MAX_BYTES = 64 * 1024
_BODY_FALLBACK_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def extract_user_token(
    request,
    *,
    allow_body_fallback: bool = True,
    allow_multipart_fallback: bool = False,
) -> Optional[str]:
    """Return X-User-Token or the temporary JSON/form body fallback.

    Query-string credentials are never accepted. Multipart fallback is opt-in
    for bounded legacy upload endpoints so authentication does not generally
    force file parsing before endpoint upload limits.
    """
    header_token = request.META.get("HTTP_X_USER_TOKEN")
    if header_token and header_token.strip():
        return header_token.strip()

    disable_at = settings.USER_KEY_BODY_FALLBACK_DISABLE_AT
    body_token = (
        _extract_legacy_body_token(
            request,
            allow_multipart_fallback=allow_multipart_fallback,
        )
        if allow_body_fallback and timezone.now() < disable_at
        else None
    )
    if body_token:
        path = getattr(request, "path", "<unknown>")
        method = getattr(request, "method", "<unknown>")
        logger.warning(
            "legacy_auth_body_fallback_used method=%s path=%s disabled_at=%s",
            method,
            path,
            disable_at.isoformat(),
        )
        return body_token

    if _token_in_query_string(request):
        logger.warning(
            "token_user supplied via query string at %s; use X-User-Token",
            getattr(request, "path", "<unknown>"),
        )
    return None


def _extract_legacy_body_token(
    request,
    *,
    allow_multipart_fallback: bool,
) -> Optional[str]:
    if getattr(request, "method", "GET").upper() not in _BODY_FALLBACK_METHODS:
        return None

    content_type = (
        getattr(request, "content_type", None)
        or request.META.get("CONTENT_TYPE", "")
    ).lower()
    is_multipart = content_type.startswith("multipart/")
    if is_multipart and not allow_multipart_fallback:
        return None

    raw_length = request.META.get("CONTENT_LENGTH")
    try:
        content_length = int(raw_length) if raw_length else 0
    except (TypeError, ValueError):
        content_length = 0
    max_bytes = (
        settings.USER_KEY_LEGACY_MULTIPART_MAX_BYTES
        if is_multipart
        else _BODY_FALLBACK_MAX_BYTES
    )
    if not raw_length or content_length > max_bytes:
        return None

    data = getattr(request, "data", None)
    if not isinstance(data, Mapping):
        return None
    token = data.get("token_user")
    if not token:
        return None
    return str(token).strip() or None


def _token_in_query_string(request) -> bool:
    if hasattr(request, "query_params"):
        return bool(request.query_params.get("token_user"))
    if hasattr(request, "GET"):
        return bool(request.GET.get("token_user"))
    return False


def resolve_user_key(request) -> UserKey:
    """Resolve the authenticated UserKey or raise AuthenticationFailed."""
    request_auth = getattr(request, "auth", None)
    if isinstance(request_auth, UserKey):
        return request_auth

    token = extract_user_token(request)
    if not token:
        raise AuthenticationFailed("Authentication token missing")
    return authenticate_access_token(token)
