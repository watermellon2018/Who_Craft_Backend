"""Tests for the auth module: token extraction, resolve_user_key, registration."""

from __future__ import annotations

import uuid

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.auth.utils import extract_user_token, resolve_user_key


class ExtractUserTokenTests(TestCase):
    """``extract_user_token`` reads the X-User-Token header first, then body.
    Query-string is intentionally ignored (only logged as a warning)."""

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, *, header=None, body=None, qs=None, method='GET'):
        path = '/api/anything/'
        if qs:
            path = f'{path}?{qs}'
        if method == 'POST':
            req = self.factory.post(path, data=body or {}, content_type='application/json')
        else:
            req = self.factory.get(path)
        if header is not None:
            req.META['HTTP_X_USER_TOKEN'] = header
        # DRF request adapter — extract_user_token reads request.data on POST.
        if body is not None:
            req.data = body
        return req

    def test_header_wins(self):
        req = self._request(header='abc-123')
        self.assertEqual(extract_user_token(req), 'abc-123')

    def test_body_used_when_no_header(self):
        req = self._request(method='POST', body={'token_user': 'body-token'})
        self.assertEqual(extract_user_token(req), 'body-token')

    def test_header_strips_whitespace(self):
        req = self._request(header='  spaced  ')
        self.assertEqual(extract_user_token(req), 'spaced')

    def test_query_string_is_ignored(self):
        # Even with ?token_user= present, the function must return None — only
        # a warning is logged. This is the security fix from P0.1.
        req = self._request(qs='token_user=leaked-token')
        with self.assertLogs('w_craft_back.auth.utils', level='WARNING'):
            self.assertIsNone(extract_user_token(req))

    def test_missing_everywhere_returns_none(self):
        req = self._request()
        self.assertIsNone(extract_user_token(req))


class ResolveUserKeyTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='alice', password='pw')
        self.user_key = UserKey.objects.create(user=self.user)

    def _request_with_header(self, token):
        req = self.factory.get('/api/anything/')
        req.META['HTTP_X_USER_TOKEN'] = str(token)
        return req

    def test_resolves_valid_token(self):
        resolved = resolve_user_key(self._request_with_header(self.user_key.key))
        self.assertEqual(resolved.pk, self.user_key.pk)
        self.assertEqual(resolved.user, self.user)

    def test_missing_token_raises_auth_failed(self):
        req = self.factory.get('/api/anything/')
        with self.assertRaises(AuthenticationFailed):
            resolve_user_key(req)

    def test_non_uuid_token_raises_auth_failed(self):
        with self.assertRaises(AuthenticationFailed):
            resolve_user_key(self._request_with_header('not-a-uuid'))

    def test_unknown_uuid_raises_auth_failed(self):
        random_uuid = uuid.uuid4()
        with self.assertRaises(AuthenticationFailed):
            resolve_user_key(self._request_with_header(random_uuid))


class RegistrationViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('register')

    def test_creates_user_and_returns_token(self):
        response = self.client.post(
            self.url,
            {'username': 'newuser', 'password': 'strong-pass-123'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertIn('token', body)
        # Token must be a valid UUID and stored as a UserKey.
        token = body['token']
        self.assertTrue(UserKey.objects.filter(key=token).exists())
        self.assertTrue(User.objects.filter(username='newuser').exists())

    def test_rejects_missing_username(self):
        response = self.client.post(
            self.url, {'password': 'whatever'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
