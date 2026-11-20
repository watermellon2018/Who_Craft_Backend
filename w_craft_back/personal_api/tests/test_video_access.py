"""Video gets an independent profile preference and cannot change its payer."""

import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings

from w_craft_back.personal_api.access import (
    credential_for_candidate, resolve_video_access,
)
from w_craft_back.personal_api.catalog import model_catalog, validate_route
from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.personal_api.services import save_preference
from w_craft_back.personal_api.models import PersonalAPIPreference
from w_craft_back.services.video_generation.registry import STATIC_MODELS
from w_craft_back.services.video_generation.tests import catalog_row, response

from .test_personal_api import FIRST_SECRET, PersonalAPIBase


class PersonalVideoAccessTests(PersonalAPIBase):
    def test_video_is_independent_and_key_never_enters_snapshot(self) -> None:
        connection = self.connection()
        self.configure(connection, modality='image')
        self.configure(connection, modality='video', model=STATIC_MODELS[0].key)
        snapshot = resolve_video_access(self.alice, requested_duration=5)
        self.assertEqual(snapshot['durationSeconds'], 6)
        access = snapshot['generationAccess']
        self.assertEqual(access['source'], 'personal')
        self.assertEqual(access['modality'], 'video')
        self.assertNotIn(FIRST_SECRET, json.dumps(snapshot))
        self.assertEqual(credential_for_candidate(access, access['candidates'][0]),
                         FIRST_SECRET)

    def test_image_only_preferences_do_not_authorize_video(self) -> None:
        connection = self.connection()
        self.configure(connection, modality='image')
        with self.assertRaises(PersonalAPIError) as error:
            resolve_video_access(self.alice, requested_duration=5)
        self.assertEqual(error.exception.code, 'CRAFT_SUBSCRIPTION_REQUIRED')

    def test_personal_mode_disallows_page_model_override(self) -> None:
        connection = self.connection()
        self.configure(connection, modality='video', model=STATIC_MODELS[0].key)
        with self.assertRaises(PersonalAPIError) as error:
            resolve_video_access(self.alice, requested_duration=5,
                                 model_key=STATIC_MODELS[-1].key)
        self.assertEqual(error.exception.code, 'VIDEO_MODEL_PROFILE_ONLY')

    def test_priority_skips_incompatible_formats(self) -> None:
        router = self.connection(provider='openrouter')
        gemini = self.connection()
        save_preference(self.alice, 'video', source='personal', routes=[
            {'connectionId': router['id'], 'modelKey': STATIC_MODELS[-1].key},
            {'connectionId': gemini['id'], 'modelKey': STATIC_MODELS[0].key},
        ])
        snapshot = resolve_video_access(
            self.alice, requested_duration=5, aspect_ratio='9:16',
        )
        self.assertEqual(snapshot['provider'], 'gemini')
        self.assertEqual(len(snapshot['generationAccess']['candidates']), 1)

    def test_fallback_does_not_change_disclosed_duration(self) -> None:
        gemini = self.connection()
        router = self.connection(provider='openrouter')
        save_preference(self.alice, 'video', source='personal', routes=[
            {'connectionId': gemini['id'], 'modelKey': STATIC_MODELS[0].key},
            {'connectionId': router['id'], 'modelKey': STATIC_MODELS[-1].key},
        ])
        snapshot = resolve_video_access(self.alice, requested_duration=5)
        self.assertEqual(snapshot['durationSeconds'], 6)
        self.assertEqual(len(snapshot['generationAccess']['candidates']), 1)

    def test_compatible_backup_pins_its_model_and_original_priority(self) -> None:
        first = self.connection()
        second = self.connection(secret='another-personal-provider-key-5678')
        save_preference(self.alice, 'video', source='personal', routes=[
            {'connectionId': first['id'], 'modelKey': STATIC_MODELS[0].key},
            {'connectionId': second['id'], 'modelKey': STATIC_MODELS[1].key},
        ])
        snapshot = resolve_video_access(self.alice, requested_duration=5)
        self.assertEqual(len(snapshot['generationAccess']['candidates']), 2)
        self.assertEqual([row['modelKey'] for row in snapshot['candidateRoutes']],
                         [STATIC_MODELS[0].key, STATIC_MODELS[1].key])
        self.assertEqual(snapshot['candidateRoutes'][1]['durationSeconds'], 6)
        self.assertEqual(snapshot['candidateRoutes'][1]['modelSpec']['provider_model'],
                         'veo-3.1-fast-generate-preview')
        self.assertNotIn('another-personal-provider-key', json.dumps(snapshot))

    def test_catalog_and_route_validation_use_same_video_ids(self) -> None:
        with patch('requests.sessions.Session.request') as call:
            videos = [row for row in model_catalog() if row['modality'] == 'video']
        call.assert_not_called()
        self.assertGreaterEqual(len(videos), 4)
        for row in videos:
            validate_route('video', row['providers'][0], row['key'])
            self.assertFalse(row['isFree'])
        with self.assertRaises(PersonalAPIError):
            validate_route('video', 'gemini', STATIC_MODELS[-1].key)

    @override_settings(OPENROUTER_API_KEY='platform-key-never-used')
    def test_explicit_refresh_uses_owner_key_and_returns_offline_settings_shape(self):
        self.connection(provider='openrouter')
        self.connection(self.bob, provider='openrouter', secret='foreign-key')
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response({'data': [catalog_row()]})) as call:
            result = self.client.post('/api/personal-api/video-models/refresh/')
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(call.call_args.args[0], 'GET')
        self.assertEqual(call.call_args.kwargs['headers'],
                         {'Authorization': f'Bearer {FIRST_SECRET}'})
        self.assertTrue(any(row['key'] == 'openrouter-video:vendor/video-model'
                            for row in result.json()['models']))
        self.assertEqual(result['Cache-Control'], 'no-store')
        self.assertNotIn(FIRST_SECRET, result.content.decode())

    def test_refresh_requires_own_active_connection(self):
        self.connection(self.bob, provider='openrouter')
        with patch('requests.sessions.Session.request') as call:
            result = self.client.post('/api/personal-api/video-models/refresh/')
        self.assertEqual(result.status_code, 403)
        call.assert_not_called()

    def test_refresh_failure_reports_error_instead_of_old_catalog_success(self):
        self.connection(provider='openrouter')
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response(status=401)):
            result = self.client.post('/api/personal-api/video-models/refresh/')
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json()['code'], 'VIDEO_AUTHENTICATION_FAILED')

    def test_save_dynamic_route_recovers_empty_process_cache(self):
        connection = self.connection(provider='openrouter')
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response({'data': [catalog_row()]})) as call:
            self.configure(connection, modality='video',
                           model='openrouter-video:vendor/video-model')
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs['headers'],
                         {'Authorization': f'Bearer {FIRST_SECRET}'})

    def test_resolve_persisted_dynamic_route_recovers_after_process_restart(self):
        connection = self.connection(provider='openrouter')
        PersonalAPIPreference.objects.create(
            user=self.alice, modality='video', source='personal', routes=[{
                'connectionId': connection['id'],
                'modelKey': 'openrouter-video:vendor/video-model',
            }],
        )
        cache.clear()
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response({'data': [catalog_row()]})) as call:
            snapshot = resolve_video_access(self.alice, requested_duration=5)
        call.assert_called_once()
        self.assertEqual(snapshot['modelKey'], 'openrouter-video:vendor/video-model')
        self.assertEqual(snapshot['durationSeconds'], 6)
