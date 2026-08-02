from __future__ import annotations

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import Throttled
from rest_framework.throttling import SimpleRateThrottle

from w_craft_back.auth.tokens import authenticate_access_token
from w_craft_back.auth.utils import extract_user_token


class LegacyBodyAuthRateThrottle(SimpleRateThrottle):
    scope = "legacy_body_auth"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            # X-Forwarded-For is client-controlled unless ingress trust is configured.
            "ident": request.META.get("REMOTE_ADDR", ""),
        }


class LegacyMultipartAuthRateThrottle(LegacyBodyAuthRateThrottle):
    scope = "legacy_multipart_auth"


def _enforce_legacy_body_rate_limit(
    request,
    view,
    *,
    allow_body_fallback: bool,
    allow_multipart_fallback: bool,
) -> None:
    header_token = request.META.get("HTTP_X_USER_TOKEN", "").strip()
    method = getattr(request, "method", "GET").upper()
    if (
        not allow_body_fallback
        or header_token
        or method not in {"POST", "PUT", "PATCH", "DELETE"}
        or timezone.now() >= settings.USER_KEY_BODY_FALLBACK_DISABLE_AT
    ):
        return

    content_type = getattr(request, "content_type", "").lower()
    is_multipart = content_type.startswith("multipart/")
    if is_multipart and not allow_multipart_fallback:
        return

    throttle_class = (
        LegacyMultipartAuthRateThrottle
        if is_multipart
        else LegacyBodyAuthRateThrottle
    )
    throttle = throttle_class()
    if not throttle.allow_request(request, view):
        raise Throttled(wait=throttle.wait())


class UserKeyAuthentication(BaseAuthentication):
    """Authenticate the existing X-User-Token bearer header for DRF."""

    allow_legacy_multipart_auth = False

    def authenticate(self, request):
        parser_context = getattr(request, "parser_context", None) or {}
        view = parser_context.get("view")
        allow_body_fallback = getattr(
            view,
            "allow_legacy_body_auth",
            True,
        )
        allow_multipart_fallback = getattr(
            view,
            "allow_legacy_multipart_auth",
            self.allow_legacy_multipart_auth,
        )
        _enforce_legacy_body_rate_limit(
            request,
            view,
            allow_body_fallback=allow_body_fallback,
            allow_multipart_fallback=allow_multipart_fallback,
        )
        raw_token = extract_user_token(
            request,
            allow_body_fallback=allow_body_fallback,
            allow_multipart_fallback=allow_multipart_fallback,
        )
        if not raw_token:
            return None
        user_key = authenticate_access_token(raw_token)
        return user_key.user, user_key

    def authenticate_header(self, request) -> str:
        return "X-User-Token"


class LegacyMultipartUserKeyAuthentication(UserKeyAuthentication):
    """Temporary bounded body-token compatibility for legacy uploads."""

    allow_legacy_multipart_auth = True
