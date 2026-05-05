import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.profile.models import UserProfile


class DashboardViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='craftuser', password='pass')
        self.user_key = UserKey.objects.create(user=self.user)
        self.token = str(self.user_key.key)
        self.url = reverse('profile-dashboard')

    def test_no_token_returns_401(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_invalid_token_returns_401(self):
        response = self.client.get(self.url, {'token_user': 'not-a-real-token'})
        self.assertEqual(response.status_code, 401)

    def test_valid_token_returns_200(self):
        response = self.client.get(self.url, {'token_user': self.token})
        self.assertEqual(response.status_code, 200)

    def test_response_contains_required_top_level_keys(self):
        response = self.client.get(self.url, {'token_user': self.token})
        data = response.json()
        for key in ('user', 'profile_completion', 'stats', 'awards',
                    'interests', 'favorite_genres', 'views_analytics',
                    'recent_activity', 'favorite_authors', 'continue_watching', 'settings'):
            self.assertIn(key, data, f'Missing key: {key}')

    def test_user_section_contains_correct_username(self):
        response = self.client.get(self.url, {'token_user': self.token})
        self.assertEqual(response.json()['user']['username'], 'craftuser')

    def test_profile_completion_structure(self):
        response = self.client.get(self.url, {'token_user': self.token})
        completion = response.json()['profile_completion']
        self.assertIn('percent', completion)
        self.assertIn('items', completion)
        self.assertIn('avatar', completion['items'])
        self.assertIn('about', completion['items'])
        self.assertIn('interests', completion['items'])
        self.assertIn('socials', completion['items'])

    def test_stats_section_has_all_fields(self):
        response = self.client.get(self.url, {'token_user': self.token})
        stats = response.json()['stats']
        for field in ('new_messages', 'subscriptions_count', 'watch_history_count',
                      'total_views', 'recommendations_count', 'completed_lessons'):
            self.assertIn(field, stats)

    def test_settings_reflect_profile_defaults(self):
        response = self.client.get(self.url, {'token_user': self.token})
        settings = response.json()['settings']
        self.assertEqual(settings['language'], 'ru')
        self.assertFalse(settings['private_account'])
        self.assertTrue(settings['notifications_enabled'])

    def test_display_name_falls_back_to_username(self):
        response = self.client.get(self.url, {'token_user': self.token})
        self.assertEqual(response.json()['user']['display_name'], 'craftuser')

    def test_display_name_uses_profile_value_when_set(self):
        UserProfile.objects.create(user=self.user, display_name='Джеймс Кэмерон')
        response = self.client.get(self.url, {'token_user': self.token})
        self.assertEqual(response.json()['user']['display_name'], 'Джеймс Кэмерон')

    def test_analytics_has_30_points(self):
        response = self.client.get(self.url, {'token_user': self.token})
        points = response.json()['views_analytics']['points']
        self.assertEqual(len(points), 30)

    def test_awards_list_is_not_empty(self):
        response = self.client.get(self.url, {'token_user': self.token})
        awards = response.json()['awards']
        self.assertGreater(len(awards), 0)
        self.assertIn('code', awards[0])
        self.assertIn('unlocked', awards[0])

    def test_token_via_header(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.status_code, 200)

    def test_creates_profile_automatically_if_missing(self):
        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())
        self.client.get(self.url, {'token_user': self.token})
        self.assertTrue(UserProfile.objects.filter(user=self.user).exists())

    def test_second_user_gets_own_data(self):
        other_user = User.objects.create_user(username='other', password='pass')
        other_key = UserKey.objects.create(user=other_user)
        UserProfile.objects.create(user=other_user, display_name='Другой пользователь')

        response = self.client.get(self.url, {'token_user': str(other_key.key)})
        self.assertEqual(response.json()['user']['username'], 'other')
        self.assertEqual(response.json()['user']['display_name'], 'Другой пользователь')


class ProfileSettingsViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='craftuser', password='pass')
        self.user_key = UserKey.objects.create(user=self.user)
        self.token = str(self.user_key.key)
        self.url = reverse('profile-settings')

    def test_no_token_returns_401(self):
        response = self.client.patch(self.url, {'language': 'en'}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_patch_language_saves_and_returns(self):
        response = self.client.patch(
            self.url,
            {'token_user': self.token, 'language': 'en'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['language'], 'en')
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.language, 'en')

    def test_patch_private_account_saves(self):
        response = self.client.patch(
            self.url,
            {'token_user': self.token, 'private_account': True},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['private_account'])
        self.assertTrue(UserProfile.objects.get(user=self.user).private_account)

    def test_patch_notifications_saves(self):
        response = self.client.patch(
            self.url,
            {'token_user': self.token, 'notifications_enabled': False},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['notifications_enabled'])

    def test_patch_multiple_fields_at_once(self):
        response = self.client.patch(
            self.url,
            {'token_user': self.token, 'language': 'en', 'private_account': True, 'notifications_enabled': False},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['language'], 'en')
        self.assertTrue(data['private_account'])
        self.assertFalse(data['notifications_enabled'])

    def test_patch_is_idempotent(self):
        for _ in range(2):
            response = self.client.patch(
                self.url,
                {'token_user': self.token, 'language': 'en'},
                format='json',
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(UserProfile.objects.get(user=self.user).language, 'en')

    def test_patch_only_updates_own_profile(self):
        other_user = User.objects.create_user(username='other', password='pass')
        other_key = UserKey.objects.create(user=other_user)
        UserProfile.objects.create(user=other_user, language='ru')

        self.client.patch(
            self.url,
            {'token_user': self.token, 'language': 'en'},
            format='json',
        )

        other_profile = UserProfile.objects.get(user=other_user)
        self.assertEqual(other_profile.language, 'ru')

    def test_response_contains_only_settings_fields(self):
        response = self.client.patch(
            self.url,
            {'token_user': self.token, 'language': 'en'},
            format='json',
        )
        data = response.json()
        self.assertIn('language', data)
        self.assertIn('private_account', data)
        self.assertIn('notifications_enabled', data)
        self.assertNotIn('bio', data)
        self.assertNotIn('token_user', data)
