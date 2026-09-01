from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project, ProjectStatus
from w_craft_back.movie.project.team_models import (
    InvitationType,
    ProjectInvitation,
)
from w_craft_back.profile.models import (
    Interest,
    UserAsset,
    UserInterest,
    UserProfile,
    UserSocialLink,
)
from w_craft_back.subscriptions.models import ChannelSubscription
from w_craft_back.subscriptions.services import subscribe


def _make_project(owner: User, title: str = 'Demo') -> Project:
    project = Project.objects.create(
        owner=owner,
        title=title,
        format='feature_film',
        annotation='',
        synopsis='legacy',
        summary='desc',
        status=ProjectStatus.IN_PROGRESS,
    )
    ProjectMember.objects.create(
        project=project,
        user=owner,
        role=ProjectMemberRole.OWNER,
        joined_at=timezone.now(),
    )
    return project


class ProfileAccountDeletionTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='alice',
            password='correct-password',
            email='alice@example.com',
            first_name='Alice',
            last_name='Craft',
        )
        self.user.last_login = timezone.now()
        self.user.save(update_fields=['last_login'])
        self.user_key = UserKey.objects.create(user=self.user)
        self.token = str(self.user_key.key)
        self.refresh_token = self.user_key.issued_tokens.refresh
        self.url = reverse('profile-me')

    def tearDown(self):
        cache.clear()

    def _delete(self, payload):
        return self.client.delete(
            self.url,
            payload,
            format='json',
            HTTP_X_USER_TOKEN=self.token,
        )

    def test_delete_requires_authentication(self):
        response = self.client.delete(
            self.url,
            {'current_password': 'correct-password'},
            format='json',
        )

        self.assertEqual(response.status_code, 401)

    def test_delete_throttle_does_not_replace_authentication_errors(self):
        for _ in range(6):
            response = self.client.delete(
                self.url,
                {'current_password': 'correct-password'},
                format='json',
            )
            self.assertEqual(response.status_code, 401)

    def test_delete_requires_non_blank_current_password(self):
        for payload in ({}, {'current_password': ''}, {'current_password': '   '}):
            with self.subTest(payload=payload):
                response = self._delete(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()['code'],
                    'ACCOUNT_DELETE_PASSWORD_REQUIRED',
                )

    def test_delete_rejects_wrong_password(self):
        response = self._delete({'current_password': 'wrong-password'})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()['code'],
            'ACCOUNT_DELETE_PASSWORD_INVALID',
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertTrue(UserKey.objects.filter(user=self.user).exists())

    def test_delete_is_blocked_while_user_owns_projects(self):
        _make_project(self.user, 'First')
        _make_project(self.user, 'Second')

        response = self._delete({'current_password': 'correct-password'})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'ACCOUNT_HAS_OWNED_PROJECTS')
        self.assertEqual(response.json()['ownedProjectCount'], 2)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.username, 'alice')

    def test_delete_is_throttled_to_five_attempts_per_hour(self):
        for _ in range(5):
            response = self._delete({'current_password': 'wrong-password'})
            self.assertEqual(response.status_code, 403)

        response = self._delete({'current_password': 'wrong-password'})

        self.assertEqual(response.status_code, 429)

    def test_delete_anonymizes_account_and_removes_personal_relations(self):
        profile = UserProfile.objects.create(
            user=self.user,
            public_username='alice_public',
            display_name='Alice Craft',
            bio='Private biography',
            subscribers_count=0,
            subscriptions_count=0,
        )
        asset = UserAsset.objects.create(
            user=self.user,
            type=UserAsset.AVATAR,
            storage_key='profiles/alice/avatar.png',
        )
        profile.avatar_asset = asset
        profile.save(update_fields=['avatar_asset', 'updated_at'])
        interest = Interest.objects.create(name='Cinema', slug='cinema')
        UserInterest.objects.create(user=self.user, interest=interest)
        UserSocialLink.objects.create(
            user=self.user,
            platform=UserSocialLink.WEBSITE,
            url='https://alice.example.com',
        )

        project_owner = User.objects.create_user(
            username='project-owner',
            password='pw',
        )
        project = _make_project(project_owner)
        ProjectMember.objects.create(
            project=project,
            user=self.user,
            role=ProjectMemberRole.EDITOR,
            joined_at=timezone.now(),
        )

        target = User.objects.create_user(username='target', password='pw')
        follower = User.objects.create_user(username='follower', password='pw')
        UserProfile.objects.create(user=target)
        UserProfile.objects.create(user=follower)
        subscribe(self.user, target.id)
        subscribe(follower, self.user.id)

        incoming_invitation = ProjectInvitation.objects.create(
            project=project,
            invited_by=project_owner,
            invited_user=self.user,
            token_hash='a' * 64,
            invitation_type=InvitationType.USERNAME,
            expires_at=timezone.now() + timedelta(days=1),
        )
        authored_invitation = ProjectInvitation.objects.create(
            project=project,
            invited_by=self.user,
            invited_user=target,
            accepted_by=self.user,
            token_hash='b' * 64,
            invitation_type=InvitationType.USERNAME,
            expires_at=timezone.now() + timedelta(days=1),
        )
        original_user_id = self.user.id

        response = self._delete({'current_password': 'correct-password'})

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')

        closed_user = User.objects.get(pk=original_user_id)
        self.assertFalse(closed_user.is_active)
        self.assertTrue(
            closed_user.username.startswith(f'deleted_user_{original_user_id}_')
        )
        self.assertNotEqual(closed_user.username, 'alice')
        self.assertEqual(closed_user.email, '')
        self.assertEqual(closed_user.first_name, '')
        self.assertEqual(closed_user.last_name, '')
        self.assertIsNone(closed_user.last_login)
        self.assertFalse(closed_user.has_usable_password())

        self.assertFalse(UserKey.objects.filter(user_id=original_user_id).exists())
        self.assertFalse(UserProfile.objects.filter(user_id=original_user_id).exists())
        self.assertFalse(UserAsset.objects.filter(user_id=original_user_id).exists())
        self.assertFalse(UserInterest.objects.filter(user_id=original_user_id).exists())
        self.assertFalse(UserSocialLink.objects.filter(user_id=original_user_id).exists())
        self.assertFalse(ProjectMember.objects.filter(user_id=original_user_id).exists())
        self.assertFalse(
            ProjectInvitation.objects.filter(pk=incoming_invitation.pk).exists()
        )
        authored_invitation.refresh_from_db()
        self.assertIsNone(authored_invitation.invited_by_id)
        self.assertIsNone(authored_invitation.accepted_by_id)
        self.assertFalse(
            ChannelSubscription.objects.filter(
                subscriber_id=original_user_id,
            ).exists()
        )
        self.assertFalse(
            ChannelSubscription.objects.filter(
                subscribed_to_id=original_user_id,
            ).exists()
        )

        target_profile = UserProfile.objects.get(user=target)
        follower_profile = UserProfile.objects.get(user=follower)
        self.assertEqual(target_profile.subscribers_count, 0)
        self.assertEqual(follower_profile.subscriptions_count, 0)

        invalidated = self.client.get(
            self.url,
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(invalidated.status_code, 401)

        refresh_response = self.client.post(
            reverse('refresh'),
            {'refresh': self.refresh_token},
            format='json',
        )
        self.assertEqual(refresh_response.status_code, 401)
