from __future__ import annotations

from typing import Optional

from rest_framework.exceptions import AuthenticationFailed

from w_craft_back.auth.models import UserKey
from w_craft_back.auth.tokens import authenticate_access_token


def extract_user_token(request) -> Optional[str]:
    """Return the access token supplied through ``X-User-Token``."""
    header_token = request.META.get("HTTP_X_USER_TOKEN")
    if header_token and header_token.strip():
        return header_token.strip()
    return None


def resolve_user_key(request) -> UserKey:
    """Resolve the authenticated UserKey or raise AuthenticationFailed."""
    request_auth = getattr(request, "auth", None)
    if isinstance(request_auth, UserKey):
        return request_auth

    token = extract_user_token(request)
    if not token:
        raise AuthenticationFailed("Authentication token missing")
    return authenticate_access_token(token)
