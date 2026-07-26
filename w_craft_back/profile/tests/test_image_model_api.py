"""Tests for ``/api/profile/me/image-model/``."""

from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.profile.models import UserProfile


class ImageModelApiTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='alice', password='pw')
        self.token = str(UserKey.objects.create(user=self.user).key)
        UserProfile.objects.create(user=self.user)
        self.url = reverse('profile-me-image-model')

    def _auth_headers(self):
        return {'HTTP_X_USER_TOKEN': self.token}

    # ---------- auth ----------

    def test_get_unauthorized(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_patch_unauthorized(self):
        response = self.client.patch(
            self.url, {'image_generation_model': 'gemini-imagen-4'}, format='json'
        )
        self.assertEqual(response.status_code, 401)

    # ---------- GET ----------

    def test_get_returns_default_for_fresh_user(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            response = self.client.get(self.url, **self._auth_headers())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['current'], 'gemini-flash-image')
        self.assertEqual(data['source'], 'default')
        self.assertIsNone(data['stored'])
        keys = {row['key'] for row in data['available']}
        self.assertIn('gemini-imagen-4', keys)
        self.assertIn('gemini-flash-image', keys)
        self.assertIn('openrouter-flash-image', keys)

    def test_get_reports_user_source_when_pref_saved(self):
        profile = self.user.profile
        profile.image_generation_model = 'gemini-flash-image'
        profile.save()
        response = self.client.get(self.url, **self._auth_headers())
        data = response.json()
        self.assertEqual(data['current'], 'gemini-flash-image')
        self.assertEqual(data['source'], 'user')
        self.assertEqual(data['stored'], 'gemini-flash-image')

    # ---------- PATCH ----------

    def test_patch_valid_key_persists(self):
        response = self.client.patch(
            self.url,
            {'image_generation_model': 'gemini-flash-image'},
            format='json',
            **self._auth_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.image_generation_model, 'gemini-flash-image')
        self.assertEqual(response.json()['current'], 'gemini-flash-image')

    def test_patch_null_resets_to_default(self):
        self.user.profile.image_generation_model = 'gemini-flash-image'
        self.user.profile.save()
        response = self.client.patch(
            self.url,
            {'image_generation_model': None},
            format='json',
            **self._auth_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.image_generation_model, '')
        self.assertEqual(response.json()['source'], 'default')

    def test_patch_unknown_key_returns_400(self):
        response = self.client.patch(
            self.url,
            {'image_generation_model': 'totally-fake'},
            format='json',
            **self._auth_headers(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'IMAGE_MODEL_UNKNOWN')

    def test_patch_missing_field_returns_400(self):
        response = self.client.patch(
            self.url, {}, format='json', **self._auth_headers(),
        )
        self.assertEqual(response.status_code, 400)
