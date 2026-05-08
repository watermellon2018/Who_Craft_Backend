from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from w_craft_back.profile.models import (
    Interest,
    UserAsset,
    UserInterest,
    UserProfile,
    UserSocialLink,
)


class UserAssetTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw')

    def test_create_avatar_asset(self):
        asset = UserAsset.objects.create(
            user=self.user,
            type=UserAsset.AVATAR,
            storage_key='users/alice/avatar.png',
            mime_type='image/png',
            size_bytes=1024,
            width=512,
            height=512,
        )
        self.assertEqual(asset.user, self.user)
        self.assertEqual(asset.type, 'avatar')
        self.assertIsNone(asset.deleted_at)


class InterestTest(TestCase):
    def test_default_interests_seeded(self):
        # seeded by migration 0021
        slugs = set(Interest.objects.values_list('slug', flat=True))
        self.assertIn('kino', slugs)
        self.assertIn('ai', slugs)
        self.assertIn('vfx', slugs)

    def test_unique_slug(self):
        Interest.objects.create(name='Музыка', slug='muzyka')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Interest.objects.create(name='Music', slug='muzyka')

    def test_auto_slug_on_save(self):
        i = Interest(name='Photography')
        i.save()
        self.assertTrue(i.slug)


class UserInterestTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='bob', password='pw')
        self.interest = Interest.objects.get(slug='ai')

    def test_attach_interest(self):
        UserInterest.objects.create(user=self.user, interest=self.interest)
        self.assertEqual(self.user.user_interests.count(), 1)

    def test_unique_per_user_interest(self):
        UserInterest.objects.create(user=self.user, interest=self.interest)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UserInterest.objects.create(user=self.user, interest=self.interest)


class UserSocialLinkTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='carol', password='pw')

    def test_create_link(self):
        link = UserSocialLink.objects.create(
            user=self.user,
            platform=UserSocialLink.TELEGRAM,
            url='https://t.me/carol',
        )
        self.assertEqual(link.display_order, 0)

    def test_unique_per_user_platform(self):
        UserSocialLink.objects.create(
            user=self.user, platform='telegram', url='https://t.me/carol'
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UserSocialLink.objects.create(
                    user=self.user, platform='telegram', url='https://t.me/another'
                )

    def test_two_users_same_platform_ok(self):
        other = User.objects.create_user(username='dan', password='pw')
        UserSocialLink.objects.create(
            user=self.user, platform='telegram', url='https://t.me/carol'
        )
        UserSocialLink.objects.create(
            user=other, platform='telegram', url='https://t.me/dan'
        )
        self.assertEqual(UserSocialLink.objects.count(), 2)


class UserProfileExtendedTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='erin', password='pw')

    def test_public_username_optional_falls_back_to_user_username(self):
        profile = UserProfile.objects.create(user=self.user)
        self.assertIsNone(profile.public_username)
        self.assertEqual(profile.effective_username, 'erin')

    def test_public_username_used_when_set(self):
        profile = UserProfile.objects.create(user=self.user, public_username='erin_public')
        self.assertEqual(profile.effective_username, 'erin_public')

    def test_public_username_unique(self):
        UserProfile.objects.create(user=self.user, public_username='shared')
        other = User.objects.create_user(username='frank', password='pw')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UserProfile.objects.create(user=other, public_username='shared')

    def test_public_username_validator_rejects_uppercase(self):
        profile = UserProfile(user=self.user, public_username='HasUpper')
        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_public_username_validator_rejects_too_short(self):
        profile = UserProfile(user=self.user, public_username='ab')
        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_avatar_asset_link(self):
        asset = UserAsset.objects.create(
            user=self.user, type='avatar', storage_key='users/erin/avatar.png'
        )
        profile = UserProfile.objects.create(user=self.user, avatar_asset=asset)
        self.assertEqual(profile.avatar_asset, asset)

    def test_avatar_asset_set_null_on_asset_delete(self):
        asset = UserAsset.objects.create(
            user=self.user, type='avatar', storage_key='users/erin/avatar.png'
        )
        profile = UserProfile.objects.create(user=self.user, avatar_asset=asset)
        asset.delete()
        profile.refresh_from_db()
        self.assertIsNone(profile.avatar_asset)

    def test_completion_uses_normalized_interests_and_socials(self):
        profile = UserProfile.objects.create(user=self.user)
        result = profile.get_profile_completion()
        self.assertFalse(result['items']['interests'])
        self.assertFalse(result['items']['socials'])

        UserInterest.objects.create(user=self.user, interest=Interest.objects.get(slug='ai'))
        UserSocialLink.objects.create(
            user=self.user, platform='telegram', url='https://t.me/erin'
        )
        result = profile.get_profile_completion()
        self.assertTrue(result['items']['interests'])
        self.assertTrue(result['items']['socials'])


class CascadeDeleteTest(TestCase):
    def test_user_delete_cascades_to_profile_assets_interests_socials(self):
        user = User.objects.create_user(username='gary', password='pw')
        UserProfile.objects.create(user=user)
        UserAsset.objects.create(user=user, type='avatar', storage_key='k')
        UserInterest.objects.create(
            user=user, interest=Interest.objects.get(slug='ai')
        )
        UserSocialLink.objects.create(
            user=user, platform='telegram', url='https://t.me/gary'
        )
        user.delete()
        self.assertFalse(UserProfile.objects.filter(user_id=user.id).exists())
        self.assertFalse(UserAsset.objects.filter(user_id=user.id).exists())
        self.assertFalse(UserInterest.objects.filter(user_id=user.id).exists())
        self.assertFalse(UserSocialLink.objects.filter(user_id=user.id).exists())
