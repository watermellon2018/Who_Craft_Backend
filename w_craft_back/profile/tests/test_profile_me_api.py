import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.profile.models import (
    Interest,
    UserAsset,
    UserInterest,
    UserProfile,
    UserSocialLink,
)


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    try:
        from PIL import Image
    except Exception:  # pragma: no cover
        raise

    buf = io.BytesIO()
    Image.new('RGB', (width, height), color=(255, 0, 0)).save(buf, format='PNG')
    return buf.getvalue()


@override_settings(MEDIA_ROOT='/tmp/craft_test_media')
class ProfileMeApiTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='alice', password='pw')
        self.token = str(UserKey.objects.create(user=self.user).key)
        self.url = reverse('profile-me')

    # ---------- auth ----------

    def test_get_me_unauthorized_returns_401(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_patch_me_unauthorized_returns_401(self):
        response = self.client.patch(self.url, {'bio': 'hi'}, format='json')
        self.assertEqual(response.status_code, 401)

    # ---------- GET ----------

    def test_get_me_returns_default_shape(self):
        response = self.client.get(self.url, {'token_user': self.token})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for key in ('user', 'interests', 'socials', 'settings', 'profile_completion'):
            self.assertIn(key, data)
        self.assertEqual(data['user']['username'], 'alice')
        self.assertEqual(data['user']['effective_username'], 'alice')
        self.assertEqual(data['interests'], [])
        self.assertEqual(data['socials'], [])

    # ---------- PATCH basic ----------

    def test_patch_me_updates_basic_fields(self):
        response = self.client.patch(
            self.url,
            {
                'display_name': 'Алиса',
                'bio': 'короткая биография',
                'location': 'Москва',
                'language': 'en',
                'private_account': True,
            },
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['user']['display_name'], 'Алиса')
        self.assertEqual(data['user']['bio'], 'короткая биография')
        self.assertEqual(data['settings']['language'], 'en')
        self.assertTrue(data['settings']['private_account'])

        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.display_name, 'Алиса')
        self.assertEqual(profile.language, 'en')

    def test_patch_public_username_invalid_format_400(self):
        response = self.client.patch(
            self.url,
            {'public_username': 'BadCase'},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 400)

    def test_patch_public_username_conflict_409(self):
        other = User.objects.create_user(username='bob', password='pw')
        UserProfile.objects.create(user=other, public_username='shared_name')

        response = self.client.patch(
            self.url,
            {'public_username': 'shared_name'},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['field'], 'public_username')

    def test_patch_public_username_valid(self):
        response = self.client.patch(
            self.url,
            {'public_username': 'alice_42'},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user']['public_username'], 'alice_42')

    # ---------- interests ----------

    def test_patch_replace_interests_creates_and_removes(self):
        # initial state via direct write
        UserInterest.objects.create(
            user=self.user, interest=Interest.objects.get(slug='ai')
        )
        UserInterest.objects.create(
            user=self.user, interest=Interest.objects.get(slug='vfx')
        )
        self.assertEqual(UserInterest.objects.filter(user=self.user).count(), 2)

        response = self.client.patch(
            self.url,
            {'interests': ['кино', 'NewTag']},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        names = set(response.json()['interests'])
        self.assertIn('кино', names)
        self.assertIn('NewTag', names)
        self.assertEqual(UserInterest.objects.filter(user=self.user).count(), 2)
        self.assertTrue(Interest.objects.filter(name='NewTag').exists())

    def test_patch_replace_interests_dedupes(self):
        response = self.client.patch(
            self.url,
            {'interests': ['кино', 'кино', 'AI']},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserInterest.objects.filter(user=self.user).count(), 2)

    def test_patch_empty_interests_clears(self):
        UserInterest.objects.create(
            user=self.user, interest=Interest.objects.get(slug='ai')
        )
        response = self.client.patch(
            self.url,
            {'interests': []},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserInterest.objects.filter(user=self.user).count(), 0)

    # ---------- socials ----------

    def test_patch_replace_socials_replaces_atomically(self):
        UserSocialLink.objects.create(
            user=self.user, platform='telegram', url='https://t.me/old'
        )
        response = self.client.patch(
            self.url,
            {
                'socials': [
                    {'platform': 'telegram', 'url': 'https://t.me/new'},
                    {'platform': 'website', 'url': 'https://alice.studio'},
                ]
            },
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        socials = list(
            UserSocialLink.objects.filter(user=self.user).order_by('display_order')
        )
        self.assertEqual(len(socials), 2)
        self.assertEqual(socials[0].platform, 'telegram')
        self.assertEqual(socials[0].url, 'https://t.me/new')
        self.assertEqual(socials[1].platform, 'website')

    def test_patch_socials_invalid_platform_400(self):
        response = self.client.patch(
            self.url,
            {'socials': [{'platform': 'myspace', 'url': 'https://myspace.com/alice'}]},
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 400)


@override_settings(MEDIA_ROOT='/tmp/craft_test_media')
class ProfileImageEndpointsTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='carol', password='pw')
        self.token = str(UserKey.objects.create(user=self.user).key)

    def _upload_avatar(self, content=None, content_type='image/png', name='a.png'):
        url = reverse('profile-me-avatar')
        upload = SimpleUploadedFile(
            name, content if content is not None else _png_bytes(), content_type=content_type
        )
        return self.client.post(
            url, {'file': upload}, format='multipart', HTTP_X_USER_TOKEN=self.token
        )

    def test_post_avatar_uploads_image_and_returns_url(self):
        response = self._upload_avatar()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn('avatar_url', body)
        self.assertIsNotNone(body['avatar_url'])
        self.assertIn('asset_id', body)

        profile = UserProfile.objects.get(user=self.user)
        self.assertTrue(bool(profile.avatar))
        self.assertIsNotNone(profile.avatar_asset_id)
        self.assertEqual(
            UserAsset.objects.filter(user=self.user, type='avatar', deleted_at__isnull=True).count(),
            1,
        )

    def test_post_avatar_replaces_previous(self):
        self._upload_avatar()
        self._upload_avatar()
        active = UserAsset.objects.filter(
            user=self.user, type='avatar', deleted_at__isnull=True
        )
        self.assertEqual(active.count(), 1)
        soft_deleted = UserAsset.objects.filter(
            user=self.user, type='avatar', deleted_at__isnull=False
        )
        self.assertEqual(soft_deleted.count(), 1)

    def test_post_avatar_too_large_413(self):
        big = b'\x00' * (5 * 1024 * 1024 + 10)
        response = self._upload_avatar(content=big, content_type='image/png')
        self.assertEqual(response.status_code, 413)

    def test_post_avatar_non_image_415(self):
        response = self._upload_avatar(
            content=b'not an image', content_type='text/plain', name='a.txt'
        )
        self.assertEqual(response.status_code, 415)

    def test_post_avatar_missing_file_400(self):
        url = reverse('profile-me-avatar')
        response = self.client.post(
            url, {}, format='multipart', HTTP_X_USER_TOKEN=self.token
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_avatar_clears_profile(self):
        self._upload_avatar()
        url = reverse('profile-me-avatar')
        response = self.client.delete(url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.status_code, 204)
        profile = UserProfile.objects.get(user=self.user)
        self.assertFalse(bool(profile.avatar))
        self.assertIsNone(profile.avatar_asset_id)
        self.assertEqual(
            UserAsset.objects.filter(user=self.user, type='avatar', deleted_at__isnull=True).count(),
            0,
        )

    def test_post_cover_uploads_image(self):
        url = reverse('profile-me-cover')
        upload = SimpleUploadedFile('c.png', _png_bytes(), content_type='image/png')
        response = self.client.post(
            url, {'file': upload}, format='multipart', HTTP_X_USER_TOKEN=self.token
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('cover_url', response.json())

    def test_delete_cover_clears_profile(self):
        url = reverse('profile-me-cover')
        upload = SimpleUploadedFile('c.png', _png_bytes(), content_type='image/png')
        self.client.post(
            url, {'file': upload}, format='multipart', HTTP_X_USER_TOKEN=self.token
        )
        response = self.client.delete(url, HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.status_code, 204)
        profile = UserProfile.objects.get(user=self.user)
        self.assertFalse(bool(profile.cover))
        self.assertIsNone(profile.cover_asset_id)

    def test_image_endpoints_unauthorized(self):
        url = reverse('profile-me-avatar')
        response = self.client.post(url, {}, format='multipart')
        self.assertEqual(response.status_code, 401)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, 401)
