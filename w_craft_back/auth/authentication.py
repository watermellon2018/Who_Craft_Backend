from __future__ import annotations

from rest_framework.authentication import BaseAuthentication

from w_craft_back.auth.tokens import authenticate_access_token
from w_craft_back.auth.utils import extract_user_token


class UserKeyAuthentication(BaseAuthentication):
    """Authenticate the existing X-User-Token bearer header for DRF."""

    def authenticate(self, request):
        raw_token = extract_user_token(request)
        if not raw_token:
            return None
        user_key = authenticate_access_token(raw_token)
        return user_key.user, user_key

    def authenticate_header(self, request) -> str:
        return "X-User-Token"
