from __future__ import annotations

import logging

from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from w_craft_back.auth.models import IssuedUserTokens, UserKey
from w_craft_back.auth.serializers import UserSerializer
from w_craft_back.auth.tokens import (
    RefreshTokenRejected,
    revoke_all_user_tokens,
    rotate_refresh_token,
    rotate_user_tokens,
)

logger = logging.getLogger(__name__)


class _AuthAnonThrottle(AnonRateThrottle):
    scope = "auth"


def _token_payload(tokens: IssuedUserTokens) -> dict:
    return {
        "status": 200,
        "access": tokens.access,
        "refresh": tokens.refresh,
        "accessExpiresAt": tokens.access_expires_at.isoformat(),
        "refreshExpiresAt": tokens.refresh_expires_at.isoformat(),
    }


class _PublicAuthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [_AuthAnonThrottle]


class RegistrationView(_PublicAuthView):
    def post(self, request):
        serializer = UserSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning("Registration failed: validation error")
            return Response(
                {"detail": "registration_failed", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            user = serializer.save(last_login=timezone.now())
            user_key = UserKey.objects.create(user=user)
            tokens = user_key.issued_tokens

        payload = _token_payload(tokens)
        payload["token"] = tokens.access
        return Response(payload, status=status.HTTP_201_CREATED)


class LoginView(_PublicAuthView):
    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        if not username or not password:
            return Response({"status": "fail"}, status=status.HTTP_400_BAD_REQUEST)

        user = authenticate(username=username, password=password)
        if user is None:
            logger.info("Login failed for unknown user")
            return Response({"status": "fail"}, status=status.HTTP_401_UNAUTHORIZED)

        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        _, tokens = rotate_user_tokens(user)
        return Response(_token_payload(tokens))


class RefreshView(_PublicAuthView):
    def post(self, request):
        raw_refresh = request.data.get("refresh")
        if not isinstance(raw_refresh, str) or not raw_refresh.strip():
            return Response(
                {"detail": "refresh token required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            _, tokens = rotate_refresh_token(raw_refresh.strip())
        except RefreshTokenRejected:
            return Response(
                {"detail": "invalid or expired refresh token"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return Response(_token_payload(tokens))


class LogoutView(APIView):
    def post(self, request):
        user_key = request.auth
        if not isinstance(user_key, UserKey):
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        revoke_all_user_tokens(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LogoutAllView(APIView):
    def post(self, request):
        if not request.user.is_authenticated:
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        revoke_all_user_tokens(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
