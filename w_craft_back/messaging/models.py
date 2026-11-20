"""Messaging persistence; all state changes serialize on the conversation."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class MessagingConversation(models.Model):
    """The unique, canonical pair of users and its ordered change counters."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_low = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='messaging_conversations_low',
    )
    user_high = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='messaging_conversations_high',
    )
    last_sequence = models.PositiveBigIntegerField(default=0)
    revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user_low', 'user_high'], name='messaging_unique_pair',
            ),
            models.CheckConstraint(
                condition=Q(user_low__lt=F('user_high')),
                name='messaging_ordered_pair',
            ),
        ]


class MessagingParticipant(models.Model):
    """Personal visibility and read state, independent from the peer's state."""

    conversation = models.ForeignKey(
        MessagingConversation,
        on_delete=models.CASCADE,
        related_name='participants',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='messaging_participations',
    )
    read_through = models.PositiveBigIntegerField(default=0)
    cleared_through = models.PositiveBigIntegerField(default=0)
    is_hidden = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'user'],
                name='messaging_unique_participant',
            ),
        ]
        indexes = [
            models.Index(
                fields=['user', 'is_hidden'], name='messaging_visible_idx',
            ),
        ]


class MessagingMessage(models.Model):
    """A message or tombstone; the original send fingerprint never changes."""

    conversation = models.ForeignKey(
        MessagingConversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='sent_messaging_messages',
    )
    sequence = models.PositiveBigIntegerField()
    text = models.TextField(max_length=4000)
    client_id = models.UUIDField()
    send_fingerprint = models.CharField(max_length=64)
    version = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'sequence'],
                name='messaging_unique_sequence',
            ),
            models.UniqueConstraint(
                fields=['conversation', 'sender', 'client_id'],
                name='messaging_unique_send',
            ),
        ]
        indexes = [
            models.Index(
                fields=['conversation', 'version'],
                name='messaging_changes_idx',
            ),
        ]
