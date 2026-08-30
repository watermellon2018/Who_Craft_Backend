from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from w_craft_back.notifications.models import EmailNotificationDelivery
from w_craft_back.notifications.services import (
    EMAIL_DELIVERY_LOCK_TTL,
    attempt_email_delivery,
)


class Command(BaseCommand):
    help = 'Retry a bounded batch of pending or failed notification emails.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        limit = options['limit']
        if limit < 1 or limit > 1000:
            raise CommandError('--limit must be between 1 and 1000')

        stale_before = timezone.now() - EMAIL_DELIVERY_LOCK_TTL
        delivery_ids = list(
            EmailNotificationDelivery.objects.filter(
                status__in=[
                    EmailNotificationDelivery.Status.PENDING,
                    EmailNotificationDelivery.Status.FAILED,
                ],
            )
            .filter(Q(locked_at__isnull=True) | Q(locked_at__lt=stale_before))
            .order_by('created_at', 'id')
            .values_list('id', flat=True)[:limit]
        )
        sent = sum(
            1 for delivery_id in delivery_ids
            if attempt_email_delivery(delivery_id)
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'Processed {len(delivery_ids)} delivery rows; sent {sent}.'
            )
        )
