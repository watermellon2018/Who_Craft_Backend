from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import transaction

from w_craft_back.movie.project import policy
from w_craft_back.movie.project.comment_models import VideoShotComment
from w_craft_back.movie.project.dashboard_models import VideoShot
from w_craft_back.notifications.models import Notification
from w_craft_back.notifications.services import NotificationEvent, dispatch_notification
from w_craft_back.profile.models import UserProfile
from w_craft_back.subscriptions.models import ChannelSubscription


@dataclass(frozen=True)
class CommentDecision:
    allowed: bool
    reason: str = ''


class CommentAccessDenied(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def comment_decision(user: User, shot: VideoShot) -> CommentDecision:
    owner = shot.project.owner
    profile, _ = UserProfile.objects.get_or_create(user=owner)
    permission = profile.comment_permission
    if permission == UserProfile.CommentPermission.NOBODY:
        return CommentDecision(False, 'COMMENTS_DISABLED')
    if permission == UserProfile.CommentPermission.FOLLOWERS:
        follows_owner = ChannelSubscription.objects.filter(
            subscriber=user,
            subscribed_to=owner,
            deleted_at__isnull=True,
        ).exists()
        if not follows_owner:
            return CommentDecision(False, 'COMMENTS_FOLLOWERS_ONLY')
    return CommentDecision(True)


def create_comment(*, user: User, shot: VideoShot, body: str) -> VideoShotComment:
    with transaction.atomic():
        locked_shot = VideoShot.objects.select_for_update().select_related(
            'project__owner',
        ).get(pk=shot.pk)
        if not policy.can_view(user, locked_shot.project):
            raise CommentAccessDenied('PROJECT_ACCESS_DENIED')
        owner_profile, _ = UserProfile.objects.select_for_update().get_or_create(
            user=locked_shot.project.owner,
        )
        if owner_profile.comment_permission == UserProfile.CommentPermission.NOBODY:
            raise CommentAccessDenied('COMMENTS_DISABLED')
        if owner_profile.comment_permission == UserProfile.CommentPermission.FOLLOWERS:
            follows_owner = ChannelSubscription.objects.filter(
                subscriber=user,
                subscribed_to=locked_shot.project.owner,
                deleted_at__isnull=True,
            ).exists()
            if not follows_owner:
                raise CommentAccessDenied('COMMENTS_FOLLOWERS_ONLY')
        comment = VideoShotComment.objects.create(
            shot=locked_shot,
            author=user,
            body=body,
        )
        owner = locked_shot.project.owner
        if owner.id != user.id:
            if owner_profile.language == 'en':
                notification_title = 'New comment'
                notification_message = (
                    f'{user.username} left a comment on Shot {locked_shot.order}.'
                )
            else:
                notification_title = 'Новый комментарий'
                notification_message = (
                    f'{user.username} оставил комментарий к '
                    f'Shot {locked_shot.order}.'
                )
            dispatch_notification(NotificationEvent(
                recipient=owner,
                type=Notification.Type.COMMENT,
                title=notification_title,
                message=notification_message,
                target_url=f'/projects/{locked_shot.project_id}',
                entity_type='video_shot_comment',
                entity_id=str(comment.id),
                idempotency_key=f'video-shot-comment:{comment.id}',
            ))
        return comment
