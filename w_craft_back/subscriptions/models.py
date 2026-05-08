import uuid

from django.contrib.auth.models import User
from django.db import models


class ChannelSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='subscriptions_made',
        db_column='subscriber_user_id',
    )
    subscribed_to = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='subscribers_relation',
        db_column='subscribed_to_user_id',
    )
    notifications_enabled = models.BooleanField(default=True)
    is_favorite = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'w_craft_back'
        db_table = 'channel_subscriptions'
        constraints = [
            models.CheckConstraint(
                check=~models.Q(subscriber=models.F('subscribed_to')),
                name='channel_subscriptions_no_self',
            ),
        ]
