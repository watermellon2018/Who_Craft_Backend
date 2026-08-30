from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.profile.models import Interest, UserInterest, UserProfile


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
        response = self.client.get(self.url, HTTP_X_USER_TOKEN='not-a-real-token')
        self.assertEqual(response.status_code, 401)

    def test_valid_token_returns_200(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.status_code, 200)

    def test_response_contains_required_top_level_keys(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        data = response.json()
        for key in ('user', 'profile_completion', 'stats', 'awards',
                    'interests', 'favorite_genres', 'views_analytics',
                    'recent_activity', 'favorite_authors', 'continue_watching', 'settings'):
            self.assertIn(key, data, f'Missing key: {key}')

    def test_user_section_contains_correct_username(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.json()['user']['username'], 'craftuser')

    def test_user_section_contains_effective_username_with_fallback(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(
            response.json()['user']['effective_username'],
            'craftuser',
        )

        profile = UserProfile.objects.get(user=self.user)
        profile.public_username = 'public_craft'
        profile.save(update_fields=['public_username', 'updated_at'])

        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(
            response.json()['user']['effective_username'],
            'public_craft',
        )

    def test_user_section_contains_saved_subscribers_count(self):
        UserProfile.objects.create(user=self.user, subscribers_count=17)

        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)

        self.assertEqual(response.json()['user']['subscribers_count'], 17)

    def test_profile_completion_structure(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        completion = response.json()['profile_completion']
        self.assertIn('percent', completion)
        self.assertIn('items', completion)
        self.assertIn('avatar', completion['items'])
        self.assertIn('about', completion['items'])
        self.assertIn('interests', completion['items'])
        self.assertIn('socials', completion['items'])

    def test_stats_section_has_all_fields(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        stats = response.json()['stats']
        self.assertFalse(stats['available'])
        for field in ('new_messages', 'subscriptions_count', 'watch_history_count',
                      'total_views', 'recommendations_count', 'completed_lessons'):
            self.assertEqual(stats[field], 0)

    def test_settings_reflect_profile_defaults(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        settings = response.json()['settings']
        self.assertEqual(settings['language'], 'ru')
        self.assertFalse(settings['private_account'])
        self.assertTrue(settings['notifications_in_app'])
        self.assertFalse(settings['notifications_email'])
        self.assertEqual(settings['content_language'], 'ru')
        self.assertEqual(settings['comment_permission'], 'everyone')

    def test_display_name_falls_back_to_username(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.json()['user']['display_name'], 'craftuser')

    def test_display_name_uses_profile_value_when_set(self):
        UserProfile.objects.create(user=self.user, display_name='Джеймс Кэмерон')
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.json()['user']['display_name'], 'Джеймс Кэмерон')

    def test_unimplemented_dashboard_sections_are_empty_or_unavailable(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        data = response.json()

        for field in ('awards', 'recent_activity', 'favorite_authors',
                      'continue_watching'):
            self.assertEqual(data[field], [])

        analytics = data['views_analytics']
        self.assertFalse(analytics['available'])
        self.assertEqual(analytics['period'], '30d')
        self.assertEqual(analytics['points'], [])
        self.assertEqual(analytics['summary'], {
            'views': 0,
            'views_delta_percent': 0,
            'unique_viewers': 0,
            'unique_viewers_delta_percent': 0,
            'average_watch_time': '',
            'average_watch_time_delta_percent': 0,
        })

    def test_default_profile_has_no_fabricated_profile_values(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        data = response.json()

        self.assertEqual(data['user']['tagline'], '')
        self.assertEqual(data['interests'], [])
        self.assertEqual(data['favorite_genres'], [])

    def test_dashboard_uses_saved_legacy_profile_values(self):
        UserProfile.objects.create(
            user=self.user,
            tagline='Снимаю документальное кино',
            interests=['Архитектура'],
            favorite_genres=['Документальный'],
        )

        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        data = response.json()

        self.assertEqual(data['user']['tagline'], 'Снимаю документальное кино')
        self.assertEqual(data['interests'], ['Архитектура'])
        self.assertEqual(data['favorite_genres'], ['Документальный'])

    def test_normalized_interests_take_precedence_over_legacy_profile_values(self):
        UserProfile.objects.create(
            user=self.user,
            interests=['Legacy interest'],
        )
        interest = Interest.objects.create(
            name='Real interest',
            slug='real-interest',
        )
        UserInterest.objects.create(user=self.user, interest=interest)

        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)

        self.assertEqual(response.json()['interests'], ['Real interest'])

    def test_token_via_header(self):
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.status_code, 200)

    def test_creates_profile_automatically_if_missing(self):
        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())
        self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertTrue(UserProfile.objects.filter(user=self.user).exists())

    def test_second_user_gets_own_data(self):
        other_user = User.objects.create_user(username='other', password='pass')
        other_key = UserKey.objects.create(user=other_user)
        UserProfile.objects.create(user=other_user, display_name='Другой пользователь')

        response = self.client.get(self.url, HTTP_X_USER_TOKEN=str(other_key.key))
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

    def test_get_returns_server_settings_without_mutation(self):
        UserProfile.objects.create(
            user=self.user,
            content_language='en',
            notifications_email=True,
            comment_permission='followers',
        )
        response = self.client.get(self.url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['content_language'], 'en')
        self.assertTrue(response.json()['notifications_email'])
        self.assertEqual(response.json()['comment_permission'], 'followers')

    def test_rejects_invalid_extensible_choice_values(self):
        response = self.client.patch(
            self.url,
            {'content_language': 'de', 'comment_permission': 'friends'},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('content_language', response.json())
        self.assertIn('comment_permission', response.json())

    def test_stale_inactive_user_cannot_mutate_settings(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.client.force_authenticate(user=self.user)

        response = self.client.patch(
            self.url,
            {'language': 'en'},
            format='json',
        )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(UserProfile.objects.filter(user=self.user).exists())

    def test_patch_language_saves_and_returns(self):
        response = self.client.patch(
            self.url,
            {'language': 'en'},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['language'], 'en')
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.language, 'en')

    def test_patch_private_account_saves(self):
        response = self.client.patch(
            self.url,
            {'private_account': True},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['private_account'])
        self.assertTrue(UserProfile.objects.get(user=self.user).private_account)

    def test_patch_notifications_saves(self):
        response = self.client.patch(
            self.url,
            {'notifications_in_app': False},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['notifications_in_app'])

    def test_patch_multiple_fields_at_once(self):
        response = self.client.patch(
            self.url,
            {
                'language': 'en',
                'private_account': True,
                'notifications_in_app': False,
                'notifications_email': True,
                'content_language': 'en',
                'comment_permission': 'followers',
            },
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['language'], 'en')
        self.assertTrue(data['private_account'])
        self.assertFalse(data['notifications_in_app'])
        self.assertTrue(data['notifications_email'])
        self.assertEqual(data['content_language'], 'en')
        self.assertEqual(data['comment_permission'], 'followers')

    def test_patch_is_idempotent(self):
        for _ in range(2):
            response = self.client.patch(
                self.url,
                {'language': 'en'},
                format='json',
                HTTP_X_USER_TOKEN=self.token,
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(UserProfile.objects.get(user=self.user).language, 'en')

    def test_patch_only_updates_own_profile(self):
        other_user = User.objects.create_user(username='other', password='pass')
        UserKey.objects.create(user=other_user)
        UserProfile.objects.create(user=other_user, language='ru')

        self.client.patch(
            self.url,
            {'language': 'en'},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )

        other_profile = UserProfile.objects.get(user=other_user)
        self.assertEqual(other_profile.language, 'ru')

    def test_response_contains_only_settings_fields(self):
        response = self.client.patch(
            self.url,
            {'language': 'en'},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        data = response.json()
        self.assertIn('language', data)
        self.assertIn('private_account', data)
        self.assertIn('notifications_in_app', data)
        self.assertIn('notifications_email', data)
        self.assertIn('content_language', data)
        self.assertIn('comment_permission', data)
        self.assertNotIn('bio', data)
        self.assertNotIn('token_user', data)
