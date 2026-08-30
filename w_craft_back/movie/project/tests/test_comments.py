from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.comment_models import VideoShotComment
from w_craft_back.movie.project.comment_service import CommentAccessDenied, create_comment
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
    Scene,
    VideoShot,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.profile.models import UserProfile
from w_craft_back.subscriptions.models import ChannelSubscription


class VideoShotCommentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_key = self._user('comment-owner')
        self.viewer, self.viewer_key = self._user('comment-viewer')
        self.outsider, self.outsider_key = self._user('comment-outsider')
        self.project = Project.objects.create(
            owner=self.owner,
            title='Comments',
            format='feature_film',
            annotation='',
            synopsis='',
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.owner,
            role=ProjectMemberRole.OWNER,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.viewer,
            role=ProjectMemberRole.VIEWER,
        )
        self.scene = Scene.objects.create(
            project=self.project,
            title='Scene',
            order=1,
            created_by=self.owner,
            updated_by=self.owner,
        )
        self.shot = VideoShot.objects.create(
            project=self.project,
            scene=self.scene,
            order=1,
            created_by=self.owner,
            updated_by=self.owner,
        )
        self.owner_profile = UserProfile.objects.create(user=self.owner)
        self.url = (
            f'/api/projects/{self.project.id}/video-shots/{self.shot.id}/comments/'
        )

    @staticmethod
    def _user(username):
        user = User.objects.create_user(username=username)
        return user, UserKey.objects.create(user=user)

    def test_everyone_with_project_access_can_comment(self):
        response = self.client.post(
            self.url,
            {'body': 'Looks good'},
            format='json',
            HTTP_X_USER_TOKEN=self.viewer_key.key,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(VideoShotComment.objects.count(), 1)

    def test_nobody_denies_even_owner_in_service_and_api(self):
        self.owner_profile.comment_permission = UserProfile.CommentPermission.NOBODY
        self.owner_profile.save(update_fields=['comment_permission'])
        with self.assertRaises(CommentAccessDenied) as raised:
            create_comment(user=self.owner, shot=self.shot, body='Owner comment')
        self.assertEqual(raised.exception.code, 'COMMENTS_DISABLED')

        response = self.client.post(
            self.url,
            {'body': 'Owner comment'},
            format='json',
            HTTP_X_USER_TOKEN=self.owner_key.key,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'COMMENTS_DISABLED')
        self.assertFalse(VideoShotComment.objects.exists())

    def test_followers_requires_active_subscription_and_project_access(self):
        self.owner_profile.comment_permission = UserProfile.CommentPermission.FOLLOWERS
        self.owner_profile.save(update_fields=['comment_permission'])
        with self.assertRaises(CommentAccessDenied) as raised:
            create_comment(user=self.viewer, shot=self.shot, body='Not following')
        self.assertEqual(raised.exception.code, 'COMMENTS_FOLLOWERS_ONLY')

        subscription = ChannelSubscription.objects.create(
            subscriber=self.viewer,
            subscribed_to=self.owner,
        )
        comment = create_comment(user=self.viewer, shot=self.shot, body='Following')
        self.assertEqual(comment.author, self.viewer)

        subscription.deleted_at = comment.created_at
        subscription.save(update_fields=['deleted_at'])
        with self.assertRaises(CommentAccessDenied):
            create_comment(user=self.viewer, shot=self.shot, body='Unfollowed')

        ChannelSubscription.objects.create(
            subscriber=self.outsider,
            subscribed_to=self.owner,
        )
        with self.assertRaises(CommentAccessDenied) as access_denied:
            create_comment(user=self.outsider, shot=self.shot, body='No project access')
        self.assertEqual(access_denied.exception.code, 'PROJECT_ACCESS_DENIED')

    def test_existing_comments_remain_when_permission_becomes_nobody(self):
        comment = create_comment(user=self.viewer, shot=self.shot, body='Existing')
        self.owner_profile.comment_permission = UserProfile.CommentPermission.NOBODY
        self.owner_profile.save(update_fields=['comment_permission'])
        response = self.client.get(
            self.url,
            HTTP_X_USER_TOKEN=self.viewer_key.key,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['can_comment'])
        self.assertEqual(response.json()['comments'][0]['id'], comment.id)
