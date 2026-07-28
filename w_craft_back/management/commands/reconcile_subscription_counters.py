from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

from w_craft_back.profile.models import UserProfile
from w_craft_back.subscriptions.models import ChannelSubscription


def _active_counts(field: str) -> dict[int, int]:
    return dict(
        ChannelSubscription.objects.filter(deleted_at__isnull=True)
        .values(field)
        .annotate(total=Count('id'))
        .values_list(field, 'total')
    )


class Command(BaseCommand):
    help = 'Check or repair denormalized active subscription counters.'

    def add_arguments(self, parser) -> None:
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument(
            '--check',
            action='store_true',
            help='Report drift and fail without modifying data.',
        )
        mode.add_argument(
            '--apply',
            action='store_true',
            help='Repair all profile counters from active subscriptions.',
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        profiles = list(
            UserProfile.objects.select_for_update().order_by('user_id')
        )
        subscriptions_by_user = _active_counts('subscriber_id')
        subscribers_by_user = _active_counts('subscribed_to_id')
        participant_ids = set(subscriptions_by_user) | set(subscribers_by_user)
        profile_user_ids = {profile.user_id for profile in profiles}
        missing_user_ids = sorted(participant_ids - profile_user_ids)

        if options['apply'] and missing_user_ids:
            UserProfile.objects.bulk_create(
                [UserProfile(user_id=user_id) for user_id in missing_user_ids],
                ignore_conflicts=True,
            )
            profiles = list(
                UserProfile.objects.select_for_update().order_by('user_id')
            )

        drifted_profiles = []
        for profile in profiles:
            expected_subscribers = subscribers_by_user.get(profile.user_id, 0)
            expected_subscriptions = subscriptions_by_user.get(profile.user_id, 0)
            if (
                profile.subscribers_count == expected_subscribers
                and profile.subscriptions_count == expected_subscriptions
            ):
                continue
            profile.subscribers_count = expected_subscribers
            profile.subscriptions_count = expected_subscriptions
            drifted_profiles.append(profile)

        if options['check']:
            if missing_user_ids or drifted_profiles:
                raise CommandError(
                    'Subscription counter drift detected: '
                    f'{len(drifted_profiles)} profile(s) differ, '
                    f'{len(missing_user_ids)} profile(s) are missing.'
                )
            self.stdout.write(
                self.style.SUCCESS('Subscription counters are consistent.')
            )
            return

        if drifted_profiles:
            UserProfile.objects.bulk_update(
                drifted_profiles,
                ['subscribers_count', 'subscriptions_count'],
            )
        self.stdout.write(
            self.style.SUCCESS(
                'Subscription counters reconciled: '
                f'{len(drifted_profiles)} profile(s) updated, '
                f'{len(missing_user_ids)} profile(s) created.'
            )
        )
