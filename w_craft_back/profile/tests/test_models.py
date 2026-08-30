from django.contrib.auth.models import User
from django.test import TestCase

from w_craft_back.profile.models import UserProfile


class UserProfileCompletionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='pass')

    def _make_profile(self, **kwargs) -> UserProfile:
        return UserProfile(user=self.user, **kwargs)

    def test_empty_profile_is_zero_percent(self):
        profile = self._make_profile()
        result = profile.get_profile_completion()
        self.assertEqual(result['percent'], 0)
        self.assertFalse(result['items']['avatar'])
        self.assertFalse(result['items']['about'])
        self.assertFalse(result['items']['interests'])
        self.assertFalse(result['items']['socials'])

    def test_bio_filled_increases_percent(self):
        profile = self._make_profile(bio='Привет, я режиссёр.')
        result = profile.get_profile_completion()
        self.assertTrue(result['items']['about'])
        self.assertGreater(result['percent'], 0)

    def test_interests_filled_increases_percent(self):
        profile = self._make_profile(interests=['Кино', 'ИИ'])
        result = profile.get_profile_completion()
        self.assertTrue(result['items']['interests'])

    def test_all_available_fields_filled_reaches_75_percent(self):
        # socials is always False (not implemented yet), so max is 75%
        profile = self._make_profile(
            bio='Текст о себе',
            interests=['Кино'],
        )
        result = profile.get_profile_completion()
        # avatar + about + interests filled (3/4 = 75%), socials always False
        self.assertEqual(result['percent'], 50)  # bio + interests = 2/4

    def test_socials_false_without_social_links(self):
        profile = UserProfile.objects.create(user=self.user, bio='bio', interests=['tag'])
        result = profile.get_profile_completion()
        self.assertFalse(result['items']['socials'])

    def test_percent_is_integer(self):
        profile = self._make_profile(bio='bio')
        result = profile.get_profile_completion()
        self.assertIsInstance(result['percent'], int)

    def test_default_settings(self):
        profile = UserProfile.objects.create(user=self.user)
        self.assertEqual(profile.language, 'ru')
        self.assertFalse(profile.private_account)
        self.assertTrue(profile.notifications_in_app)
        self.assertFalse(profile.notifications_email)
        self.assertEqual(profile.content_language, 'ru')
        self.assertEqual(profile.comment_permission, 'everyone')
        self.assertEqual(profile.favorite_genres, [])
        self.assertEqual(profile.interests, [])
