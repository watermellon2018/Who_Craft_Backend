"""Security and lifecycle tests for UserKeyAuthentication."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient
from rest_framework.throttling import UserRateThrottle

from w_craft_back.auth.authentication import (
    LegacyBodyAuthRateThrottle,
)
from w_craft_back.auth.models import UserKey, digest_token
from w_craft_back.auth.utils import extract_user_token, resolve_user_key


class ExtractUserTokenTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, *, header=None, body=None, qs=None, method="GET"):
        path = "/api/anything/"
        if qs:
            path = f"{path}?{qs}"
        if method == "POST":
            request = self.factory.post(
                path,
                data=body or {},
                content_type="application/json",
            )
        else:
            request = self.factory.get(path)
        if header is not None:
            request.META["HTTP_X_USER_TOKEN"] = header
        if body is not None:
            request.data = body
        return request

    def test_header_wins_and_strips_whitespace(self):
        request = self._request(
            header="  header-token  ",
            method="POST",
            body={"token_user": "body-token"},
        )
        self.assertEqual(extract_user_token(request), "header-token")

    def test_body_fallback_emits_telemetry(self):
        request = self._request(
            method="POST",
            body={"token_user": "body-token"},
        )
        with self.assertLogs("w_craft_back.auth.utils", level="WARNING") as logs:
            self.assertEqual(extract_user_token(request), "body-token")
        self.assertIn("legacy_auth_body_fallback_used", logs.output[0])
        self.assertNotIn("body-token", logs.output[0])

    def test_body_fallback_stops_at_deadline(self):
        request = self._request(
            method="POST",
            body={"token_user": "body-token"},
        )
        with override_settings(
            USER_KEY_BODY_FALLBACK_DISABLE_AT=timezone.now() - timedelta(seconds=1)
        ), patch(
            "w_craft_back.auth.utils._extract_legacy_body_token"
        ) as extractor:
            self.assertIsNone(extract_user_token(request))
        extractor.assert_not_called()

    def test_multipart_body_fallback_is_not_parsed(self):
        request = self.factory.post(
            "/api/anything/",
            data={"token_user": "body-token", "file": "payload"},
        )
        request.data = {"token_user": "body-token"}
        self.assertIsNone(extract_user_token(request))

    def test_bounded_multipart_body_fallback_can_be_opted_in(self):
        request = self.factory.post(
            "/api/anything/",
            data={"token_user": "body-token", "file": "payload"},
        )
        request.data = {"token_user": "body-token"}
        with self.assertLogs("w_craft_back.auth.utils", level="WARNING"):
            token = extract_user_token(
                request,
                allow_multipart_fallback=True,
            )
        self.assertEqual(token, "body-token")

    def test_oversized_multipart_body_fallback_is_rejected(self):
        request = self.factory.post(
            "/api/anything/",
            data={"token_user": "body-token", "file": "payload"},
        )
        request.data = {"token_user": "body-token"}
        with override_settings(USER_KEY_LEGACY_MULTIPART_MAX_BYTES=1):
            self.assertIsNone(
                extract_user_token(
                    request,
                    allow_multipart_fallback=True,
                )
            )

    def test_body_fallback_without_content_length_is_rejected(self):
        request = self.factory.post(
            "/api/anything/",
            data={"token_user": "body-token"},
        )
        request.META.pop("CONTENT_LENGTH", None)
        request.data = {"token_user": "body-token"}
        self.assertIsNone(
            extract_user_token(request, allow_multipart_fallback=True)
        )

    def test_query_string_is_ignored(self):
        request = self._request(qs="token_user=leaked-token")
        with self.assertLogs("w_craft_back.auth.utils", level="WARNING"):
            self.assertIsNone(extract_user_token(request))

    def test_missing_everywhere_returns_none(self):
        self.assertIsNone(extract_user_token(self._request()))


class ResolveUserKeyTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.user_key = UserKey.objects.create(user=self.user)
        self.access = self.user_key.key

    def _request_with_header(self, token):
        request = self.factory.get("/api/anything/")
        request.META["HTTP_X_USER_TOKEN"] = str(token)
        return request

    def test_resolves_valid_digest_backed_token(self):
        resolved = resolve_user_key(self._request_with_header(self.access))
        self.assertEqual(resolved.pk, self.user_key.pk)
        self.assertEqual(resolved.user, self.user)

    def test_plaintext_token_is_not_recoverable_from_database(self):
        stored = UserKey.objects.get(pk=self.user_key.pk)
        self.assertEqual(stored.key_digest, digest_token(self.access))
        self.assertNotEqual(stored.key_digest, self.access)
        with self.assertRaises(AttributeError):
            _ = stored.key

    def test_missing_or_unknown_token_is_rejected(self):
        with self.assertRaises(AuthenticationFailed):
            resolve_user_key(self.factory.get("/api/anything/"))
        with self.assertRaises(AuthenticationFailed):
            resolve_user_key(self._request_with_header("unknown-token"))

    def test_expired_token_is_rejected(self):
        self.user_key.expires_at = timezone.now() - timedelta(seconds=1)
        self.user_key.save(update_fields=["expires_at"])
        with self.assertRaises(AuthenticationFailed):
            resolve_user_key(self._request_with_header(self.access))

    def test_revoked_token_is_rejected(self):
        self.user_key.revoke()
        with self.assertRaises(AuthenticationFailed):
            resolve_user_key(self._request_with_header(self.access))


class AuthLifecycleViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="auth-user",
            password="strong-pass-123",
        )
        self.user_key = UserKey.objects.create(user=self.user)
        self.old_access = self.user_key.key
        self.old_refresh = self.user_key.issued_tokens.refresh

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_login_rotates_distinct_access_and_refresh_tokens(self):
        response = self.client.post(
            reverse("login"),
            {"username": self.user.username, "password": "strong-pass-123"},
            format="json",
            HTTP_X_USER_TOKEN="stale-token-is-ignored-on-public-auth",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertNotEqual(body["access"], body["refresh"])
        self.assertNotEqual(body["access"], self.old_access)

        self.assertEqual(
            self.client.get(
                reverse("profile-me"),
                HTTP_X_USER_TOKEN=self.old_access,
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.get(
                reverse("profile-me"),
                HTTP_X_USER_TOKEN=body["access"],
            ).status_code,
            status.HTTP_200_OK,
        )

    def test_refresh_is_one_time_and_rotates_both_tokens(self):
        response = self.client.post(
            reverse("refresh"),
            {"refresh": self.old_refresh},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertNotEqual(body["access"], self.old_access)
        self.assertNotEqual(body["refresh"], self.old_refresh)
        self.assertNotEqual(body["access"], body["refresh"])

        replay = self.client.post(
            reverse("refresh"),
            {"refresh": self.old_refresh},
            format="json",
        )
        self.assertEqual(replay.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_revokes_access_and_refresh(self):
        response = self.client.post(
            reverse("logout"),
            HTTP_X_USER_TOKEN=self.old_access,
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(
            self.client.get(
                reverse("profile-me"),
                HTTP_X_USER_TOKEN=self.old_access,
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertEqual(
            self.client.post(
                reverse("refresh"),
                {"refresh": self.old_refresh},
                format="json",
            ).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_protected_api_is_default_deny(self):
        response = self.client.get(reverse("profile-me"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class LegacyBodyAuthRateThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user_key = UserKey.objects.create(
            user=User.objects.create_user(username="legacy-body-throttle")
        )
        self.access = self.user_key.key

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_body_fallback_is_limited_without_affecting_header_auth(self):
        with patch.object(
            LegacyBodyAuthRateThrottle,
            "rate",
            "1/min",
            create=True,
        ):
            first = self.client.patch(
                reverse("profile-me"),
                {"token_user": self.access, "bio": "first"},
                format="json",
            )
            blocked = self.client.patch(
                reverse("profile-me"),
                {"token_user": self.access, "bio": "second"},
                format="json",
            )
            header = self.client.patch(
                reverse("profile-me"),
                {"bio": "header"},
                format="json",
                HTTP_X_USER_TOKEN=self.access,
            )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(header.status_code, status.HTTP_200_OK)

    def test_changing_forwarded_for_does_not_bypass_limit(self):
        with patch.object(
            LegacyBodyAuthRateThrottle,
            "rate",
            "1/min",
            create=True,
        ):
            first = self.client.patch(
                reverse("profile-me"),
                {"token_user": self.access, "bio": "first"},
                format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.1",
            )
            blocked = self.client.patch(
                reverse("profile-me"),
                {"token_user": self.access, "bio": "second"},
                format="json",
                HTTP_X_FORWARDED_FOR="203.0.113.2",
            )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class RegistrationViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_creates_user_and_returns_digest_backed_token_pair(self):
        response = self.client.post(
            reverse("register"),
            {"username": "newuser", "password": "strong-pass-123"},
            format="json",
            HTTP_X_USER_TOKEN="expired-token-must-not-block-registration",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body["token"], body["access"])
        self.assertNotEqual(body["access"], body["refresh"])

        user_key = UserKey.objects.get(user__username="newuser")
        self.assertEqual(user_key.key_digest, digest_token(body["access"]))
        self.assertEqual(user_key.refresh_digest, digest_token(body["refresh"]))

    def test_rejects_missing_username(self):
        response = self.client.post(
            reverse("register"),
            {"password": "whatever"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class UserRateThrottleIsolationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.first = UserKey.objects.create(
            user=User.objects.create_user(username="throttle-first")
        )
        self.second = UserKey.objects.create(
            user=User.objects.create_user(username="throttle-second")
        )
        self.first_token = self.first.key
        self.second_token = self.second.key

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_authenticated_users_do_not_share_the_ip_bucket(self):
        with patch.object(UserRateThrottle, "rate", "1/min", create=True):
            first_response = self.client.get(
                reverse("profile-me"),
                HTTP_X_USER_TOKEN=self.first_token,
            )
            first_limited = self.client.get(
                reverse("profile-me"),
                HTTP_X_USER_TOKEN=self.first_token,
            )
            second_response = self.client.get(
                reverse("profile-me"),
                HTTP_X_USER_TOKEN=self.second_token,
            )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(first_limited.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
