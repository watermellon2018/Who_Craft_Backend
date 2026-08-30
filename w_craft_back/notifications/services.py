from dataclasses import dataclass
from datetime import timedelta
from typing import Optional
import uuid

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from w_craft_back.profile.models import UserProfile

from .email import send_notification_email
from .models import (
    EmailNotificationDelivery,
    Notification,
    NotificationDispatchReceipt,
)


EMAIL_DELIVERY_LOCK_TTL = timedelta(minutes=5)
EMAIL_ERROR_MAX_LENGTH = 2000


@dataclass(frozen=True)
class NotificationEvent:
    recipient: User
    type: str
    title: str
    message: str = ''
    target_url: str = ''
    entity_type: str = ''
    entity_id: str = ''
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class DispatchResult:
    notification: Optional[Notification]
    email_scheduled: bool
    email_delivery: Optional[EmailNotificationDelivery] = None


def attempt_email_delivery(delivery_id: int) -> bool:
    """Claim and attempt one durable email delivery."""
    now = timezone.now()
    with transaction.atomic():
        delivery = (
            EmailNotificationDelivery.objects.select_for_update(skip_locked=True)
            .filter(
                pk=delivery_id,
                status__in=[
                    EmailNotificationDelivery.Status.PENDING,
                    EmailNotificationDelivery.Status.FAILED,
                ],
            )
            .first()
        )
        if delivery is None:
            return False
        if (
            delivery.locked_at is not None
            and delivery.locked_at > now - EMAIL_DELIVERY_LOCK_TTL
        ):
            return False
        delivery.locked_at = now
        delivery.last_attempt_at = now
        delivery.attempts += 1
        delivery.save(
            update_fields=[
                'locked_at',
                'last_attempt_at',
                'attempts',
                'updated_at',
            ]
        )

    try:
        send_notification_email(
            recipient_email=delivery.recipient_email,
            title=delivery.title,
            message=delivery.message,
        )
    except Exception as exc:  # SMTP/backend exceptions vary by adapter.
        EmailNotificationDelivery.objects.filter(pk=delivery_id).exclude(
            status=EmailNotificationDelivery.Status.SENT,
        ).update(
            status=EmailNotificationDelivery.Status.FAILED,
            last_error=str(exc)[:EMAIL_ERROR_MAX_LENGTH],
            locked_at=None,
            updated_at=timezone.now(),
        )
        return False

    EmailNotificationDelivery.objects.filter(pk=delivery_id).update(
        status=EmailNotificationDelivery.Status.SENT,
        last_error='',
        locked_at=None,
        sent_at=timezone.now(),
        updated_at=timezone.now(),
    )
    return True


def _schedule_email_delivery(delivery: EmailNotificationDelivery) -> bool:
    if delivery.status == EmailNotificationDelivery.Status.SENT:
        return False
    transaction.on_commit(
        lambda delivery_id=delivery.id: attempt_email_delivery(delivery_id),
        robust=True,
    )
    return True


@transaction.atomic
def dispatch_notification(event: NotificationEvent) -> DispatchResult:
    """Apply preferences once and fan one event out to durable channels."""
    if event.target_url and (
        not event.target_url.startswith('/') or event.target_url.startswith('//')
    ):
        raise ValueError('notification target_url must be an internal relative URL')

    dispatch_key = event.idempotency_key or f'notification:{uuid.uuid4().hex}'
    receipt, should_dispatch = NotificationDispatchReceipt.objects.get_or_create(
        idempotency_key=dispatch_key,
    )
    if not should_dispatch:
        delivery = EmailNotificationDelivery.objects.filter(
            dispatch_receipt=receipt,
        ).first()
        email_scheduled = bool(delivery and _schedule_email_delivery(delivery))
        notification = None
        if event.idempotency_key:
            notification = Notification.objects.filter(
                idempotency_key=event.idempotency_key,
            ).first()
        return DispatchResult(
            notification=notification,
            email_scheduled=email_scheduled,
            email_delivery=delivery,
        )

    profile, _ = UserProfile.objects.get_or_create(user=event.recipient)
    notification = None
    if profile.notifications_in_app:
        values = {
            'recipient': event.recipient,
            'type': event.type,
            'title': event.title,
            'message': event.message,
            'target_url': event.target_url,
            'entity_type': event.entity_type,
            'entity_id': event.entity_id,
        }
        if event.idempotency_key:
            notification, _ = Notification.objects.get_or_create(
                idempotency_key=event.idempotency_key,
                defaults=values,
            )
        else:
            notification = Notification.objects.create(**values)

    delivery = None
    should_email = bool(profile.notifications_email and event.recipient.email)
    if should_email:
        delivery = EmailNotificationDelivery.objects.create(
            dispatch_receipt=receipt,
            recipient=event.recipient,
            recipient_email=event.recipient.email,
            title=event.title,
            message=event.message,
        )
        _schedule_email_delivery(delivery)
    return DispatchResult(
        notification=notification,
        email_scheduled=should_email,
        email_delivery=delivery,
    )
