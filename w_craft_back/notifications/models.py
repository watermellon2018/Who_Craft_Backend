from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Type(models.TextChoices):
        PROJECT_INVITATION = 'project_invitation', 'Project invitation'
        COMMENT = 'comment', 'Comment'
        GENERATION = 'generation', 'Generation'
        SYSTEM = 'system', 'System'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    type = models.CharField(max_length=64, choices=Type.choices)
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default='')
    target_url = models.CharField(max_length=1024, blank=True, default='')
    entity_type = models.CharField(max_length=64, blank=True, default='')
    entity_id = models.CharField(max_length=128, blank=True, default='')
    idempotency_key = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        unique=True,
    )
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_notifications'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(
                fields=['recipient', 'is_read', 'created_at'],
                name='notify_recipient_read_idx',
            ),
        ]


class NotificationDispatchReceipt(models.Model):
    """Idempotency boundary shared by all delivery channels for one event."""

    idempotency_key = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notification_dispatch_receipts'


class EmailNotificationDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        FAILED = 'failed', 'Failed'
        SENT = 'sent', 'Sent'

    dispatch_receipt = models.OneToOneField(
        NotificationDispatchReceipt,
        on_delete=models.CASCADE,
        related_name='email_delivery',
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_email_deliveries',
    )
    recipient_email = models.EmailField(max_length=254)
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default='')
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default='')
    locked_at = models.DateTimeField(blank=True, null=True)
    last_attempt_at = models.DateTimeField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'email_notification_deliveries'
        indexes = [
            models.Index(
                fields=['status', 'locked_at', 'created_at'],
                name='email_delivery_retry_idx',
            ),
        ]
