from django.conf import settings
from django.db import models

from w_craft_back.movie.project.dashboard_models import VideoShot


class VideoShotComment(models.Model):
    shot = models.ForeignKey(
        VideoShot,
        on_delete=models.CASCADE,
        related_name='comments',
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='video_shot_comments',
    )
    body = models.TextField(max_length=4000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'video_shot_comments'
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['shot', 'created_at'], name='shot_comment_created_idx'),
        ]
