from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


@dataclass(frozen=True)
class IssuedUserTokens:
    access: str
    refresh: str
    access_expires_at: datetime
    refresh_expires_at: datetime


def digest_token(raw_token: str) -> str:
    """Return the stable digest stored for a high-entropy bearer token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _access_ttl() -> timedelta:
    return getattr(settings, "USER_KEY_ACCESS_TTL", timedelta(hours=1))


def _refresh_ttl() -> timedelta:
    return getattr(settings, "USER_KEY_REFRESH_TTL", timedelta(days=30))


class UserKey(models.Model):
    """Revocable access/refresh credential pair for one Django user.

    Raw bearer tokens are returned only when issued. The database stores their
    SHA-256 digests, so a database read cannot be used directly as a credential.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    key_digest = models.CharField(max_length=64, unique=True)
    refresh_digest = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
    )
    expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    rotated_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if self._state.adding and not self.key_digest:
            self._assign_new_tokens()
        return super().save(*args, **kwargs)

    @property
    def key(self) -> str:
        """Compatibility accessor for a token issued on this instance only."""
        raw_token = getattr(self, "_raw_access_token", None)
        if raw_token is None:
            raise AttributeError(
                "Raw UserKey token is not stored; rotate tokens to issue one."
            )
        return raw_token

    @property
    def issued_tokens(self) -> IssuedUserTokens:
        access = getattr(self, "_raw_access_token", None)
        refresh = getattr(self, "_raw_refresh_token", None)
        if access is None or refresh is None:
            raise AttributeError(
                "Raw tokens are available only immediately after issue."
            )
        return IssuedUserTokens(
            access=access,
            refresh=refresh,
            access_expires_at=self.expires_at,
            refresh_expires_at=self.refresh_expires_at,
        )

    def rotate_tokens(self) -> IssuedUserTokens:
        """Replace both bearer tokens and clear any previous revocation."""
        self._assign_new_tokens()
        self.save(
            update_fields=[
                "key_digest",
                "refresh_digest",
                "expires_at",
                "refresh_expires_at",
                "revoked_at",
                "rotated_at",
            ]
        )
        return self.issued_tokens

    def revoke(self) -> None:
        """Revoke both access and refresh credentials immediately."""
        self.revoked_at = timezone.now()
        self._raw_access_token = None
        self._raw_refresh_token = None
        self.save(update_fields=["revoked_at"])

    def _assign_new_tokens(self) -> None:
        now = timezone.now()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(48)
        self.key_digest = digest_token(access)
        self.refresh_digest = digest_token(refresh)
        self.expires_at = now + _access_ttl()
        self.refresh_expires_at = now + _refresh_ttl()
        self.revoked_at = None
        self.rotated_at = now
        self._raw_access_token = access
        self._raw_refresh_token = refresh
