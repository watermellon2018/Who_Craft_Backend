"""Tests for ``/api/profile/me/image-model/``."""

from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.profile.models import UserProfile
from w_craft_back.services.image_generation.errors import (
    CODE_UNAVAILABLE,
    ImageProviderError,
)
from w_craft_back.services.image_generation.registry import ModelSpec


class ImageModelApiTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='alice', password='pw')
        self.user_key = UserKey.objects.create(user=self.user)
        self.token = str(self.user_key.key)
        UserProfile.objects.create(user=self.user)
        self.url = reverse('profile-me-image-model')
        self.catalog_patcher = mock.patch(
            'w_craft_back.services.image_generation.registry._dynamic_specs',
            return_value=[],
        )
        self.catalog_patcher.start()
        self.addCleanup(self.catalog_patcher.stop)

    def _auth_headers(self):
        return {'HTTP_X_USER_TOKEN': self.token}

    def _project(self, *, owner=None, generation_settings=None):
        owner = owner or self.user
        owner_key = UserKey.objects.get(user=owner)
        return Project.objects.create(
            user=owner_key,
            owner=owner,
            title='Film',
            format='series',
            annot='Short',
            desc='Long',
            generation_settings=generation_settings or {},
        )

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
        required_fields = {
            'key', 'label', 'description', 'backend', 'model_id', 'mode',
            'supports_generate', 'supports_edit', 'supports_reference',
            'supported_parameters', 'input_modalities', 'output_modalities',
            'default', 'configured', 'requires_env',
        }
        self.assertTrue(all(required_fields <= set(row) for row in data['available']))

    def test_get_reports_user_source_when_pref_saved(self):
        profile = self.user.profile
        profile.image_generation_model = 'gemini-flash-image'
        profile.save()
        response = self.client.get(self.url, **self._auth_headers())
        data = response.json()
        self.assertEqual(data['current'], 'gemini-flash-image')
        self.assertEqual(data['source'], 'user')
        self.assertEqual(data['stored'], 'gemini-flash-image')

    def test_get_without_project_keeps_profile_default_behavior(self):
        with mock.patch.dict(
            os.environ,
            {
                'CHARACTER_STUDIO_IMAGE_PROVIDER': 'mock',
                'DEFAULT_IMAGE_MODEL': '',
            },
            clear=False,
        ):
            response = self.client.get(self.url, **self._auth_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.json()['current'], response.json()['source']),
            ('gemini-flash-image', 'default'),
        )

    def test_get_project_override_wins_over_profile(self):
        profile = self.user.profile
        profile.image_generation_model = 'gemini-flash-image'
        profile.save(update_fields=['image_generation_model'])
        project = self._project(
            generation_settings={'image_generation_model': 'mock'}
        )

        response = self.client.get(
            self.url,
            {'project_id': project.id},
            **self._auth_headers(),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual((data['current'], data['source']), ('mock', 'project'))
        self.assertTrue(data['configured'])
        mock_row = next(row for row in data['available'] if row['key'] == 'mock')
        self.assertTrue(mock_row['supports_reference'])

    def test_get_project_uses_character_environment_mock(self):
        project = self._project()
        with mock.patch.dict(
            os.environ,
            {'CHARACTER_STUDIO_IMAGE_PROVIDER': 'mock'},
            clear=False,
        ):
            response = self.client.get(
                self.url,
                {'project_id': project.id},
                **self._auth_headers(),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.json()['current'], response.json()['source']),
            ('mock', 'env'),
        )

    def test_get_project_reports_unconfigured_selected_model(self):
        project = self._project(
            generation_settings={'image_generation_model': 'gemini-flash-image'}
        )
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': ''}, clear=False):
            response = self.client.get(
                self.url,
                {'project_id': project.id},
                **self._auth_headers(),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.json()['current'], response.json()['source']),
            ('gemini-flash-image', 'project'),
        )
        self.assertFalse(response.json()['configured'])

    def test_get_project_allows_viewer_member(self):
        owner = User.objects.create_user(username='project-owner', password='pw')
        UserKey.objects.create(user=owner)
        project = self._project(owner=owner)
        ProjectMember.objects.create(
            project=project,
            user=self.user,
            role=ProjectMemberRole.VIEWER,
        )
        with mock.patch.dict(
            os.environ,
            {'CHARACTER_STUDIO_IMAGE_PROVIDER': 'mock'},
            clear=False,
        ):
            response = self.client.get(
                self.url,
                {'project_id': project.id},
                **self._auth_headers(),
            )
        self.assertEqual(response.status_code, 200)

    def test_get_project_rejects_inaccessible_project(self):
        owner = User.objects.create_user(username='private-owner', password='pw')
        UserKey.objects.create(user=owner)
        project = self._project(owner=owner)
        response = self.client.get(
            self.url,
            {'project_id': project.id},
            **self._auth_headers(),
        )
        self.assertEqual(response.status_code, 403)

    def test_get_project_validates_identifier_and_not_found(self):
        invalid = self.client.get(
            self.url,
            {'project_id': 'not-an-id'},
            **self._auth_headers(),
        )
        missing = self.client.get(
            self.url,
            {'project_id': 2147483647},
            **self._auth_headers(),
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(missing.status_code, 404)

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
        with mock.patch.dict(os.environ, {'DEFAULT_IMAGE_MODEL': ''}, clear=False):
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

    def test_patch_dynamic_catalog_key_persists(self):
        spec = ModelSpec(
            key='openrouter-images:openai/gpt-image-2',
            label='GPT Image 2',
            backend='openrouter-images',
            model_id='openai/gpt-image-2',
            mode='images',
            supports_generate=True,
            supports_edit=True,
            supports_reference=True,
            requires_env=('OPENROUTER_API_KEY',),
        )
        with mock.patch(
            'w_craft_back.services.image_generation.registry._dynamic_specs',
            return_value=[spec],
        ):
            response = self.client.patch(
                self.url,
                {'image_generation_model': spec.key},
                format='json',
                **self._auth_headers(),
            )
        self.assertEqual(response.status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.image_generation_model, spec.key)

    def test_patch_dynamic_key_fails_controlled_when_catalog_unavailable(self):
        unavailable = ImageProviderError(
            code=CODE_UNAVAILABLE,
            message='catalog unavailable',
            http_status=503,
        )
        with mock.patch(
            'w_craft_back.services.image_generation.registry._dynamic_specs',
            side_effect=unavailable,
        ):
            response = self.client.patch(
                self.url,
                {'image_generation_model': 'openrouter-images:openai/gpt-image-2'},
                format='json',
                **self._auth_headers(),
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], CODE_UNAVAILABLE)

    def test_patch_rejects_model_without_supported_raster_output(self):
        spec = ModelSpec(
            key='openrouter-images:recraft/vector-model',
            label='Vector model',
            backend='openrouter-images',
            model_id='recraft/vector-model',
            mode='images',
            supports_generate=False,
            supports_edit=False,
            supports_reference=False,
            output_modalities=('image',),
            requires_env=('OPENROUTER_API_KEY',),
        )
        with mock.patch(
            'w_craft_back.services.image_generation.registry._dynamic_specs',
            return_value=[spec],
        ):
            response = self.client.patch(
                self.url,
                {'image_generation_model': spec.key},
                format='json',
                **self._auth_headers(),
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['code'],
            'IMAGE_PROVIDER_GENERATE_NOT_SUPPORTED',
        )

    def test_get_degrades_to_static_catalog_when_discovery_unavailable(self):
        unavailable = ImageProviderError(
            code=CODE_UNAVAILABLE,
            message='catalog unavailable',
            http_status=503,
        )
        with mock.patch(
            'w_craft_back.services.image_generation.registry._dynamic_specs',
            side_effect=unavailable,
        ):
            response = self.client.get(self.url, **self._auth_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {row['key'] for row in response.json()['available']},
            {
                'gemini-imagen-4',
                'gemini-flash-image',
                'openrouter-flash-image',
                'gemini-native',
            },
        )

    def test_patch_missing_field_returns_400(self):
        response = self.client.patch(
            self.url, {}, format='json', **self._auth_headers(),
        )
        self.assertEqual(response.status_code, 400)
