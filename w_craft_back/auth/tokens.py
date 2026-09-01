from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed

from w_craft_back.auth.models import IssuedUserTokens, UserKey, digest_token


class RefreshTokenRejected(Exception):
    """Raised when a refresh credential is missing, expired, or revoked."""


def authenticate_access_token(raw_token: str) -> UserKey:
    """Resolve an active access token or raise DRF AuthenticationFailed."""
    user_key = (
        UserKey.objects.select_related("user")
        .filter(key_digest=digest_token(raw_token))
        .first()
    )
    now = timezone.now()
    if (
        user_key is None
        or user_key.revoked_at is not None
        or user_key.expires_at <= now
        or not user_key.user.is_active
    ):
        raise AuthenticationFailed("Invalid or expired authentication token")

    if user_key.last_used_at is None or user_key.last_used_at < now - timedelta(
        minutes=5
    ):
        UserKey.objects.filter(pk=user_key.pk).update(last_used_at=now)
        user_key.last_used_at = now
    return user_key


def rotate_user_tokens(user: User) -> tuple[UserKey, IssuedUserTokens]:
    """Issue a new access/refresh pair and invalidate the previous pair."""
    with transaction.atomic():
        # Serialize first login/token-row creation for the same account.
        # The UserKey lock below also serializes login against refresh.
        user = User.objects.select_for_update().get(pk=user.pk)
        UserKey.objects.get_or_create(user=user)
        user_key = UserKey.objects.select_for_update().get(user=user)
        return user_key, user_key.rotate_tokens()


def rotate_refresh_token(raw_token: str) -> tuple[UserKey, IssuedUserTokens]:
    """Consume a refresh credential once and atomically rotate both tokens."""
    with transaction.atomic():
        user_key = (
            UserKey.objects.select_for_update()
            .select_related("user")
            .filter(refresh_digest=digest_token(raw_token))
            .first()
        )
        now = timezone.now()
        if (
            user_key is None
            or user_key.revoked_at is not None
            or user_key.refresh_expires_at is None
            or user_key.refresh_expires_at <= now
            or not user_key.user.is_active
        ):
            raise RefreshTokenRejected()
        return user_key, user_key.rotate_tokens()


def revoke_all_user_tokens(user: User) -> bool:
    """Revoke the user's current credential pair, if one exists.

    ``UserKey`` is one-to-one with ``User`` and login/refresh always rotates that
    pair, so revoking this row invalidates every access and refresh credential
    that may still be held by any device.
    """
    with transaction.atomic():
        user_key = UserKey.objects.select_for_update().filter(user=user).first()
        if user_key is None:
            return False
        user_key.revoke()
        return True
