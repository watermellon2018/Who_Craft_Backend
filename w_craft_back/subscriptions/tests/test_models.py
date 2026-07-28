from django.contrib.auth.models import User
from django.db import IntegrityError, models, transaction
from django.test import TestCase
from django.utils import timezone

from w_craft_back.subscriptions.models import ChannelSubscription


class ChannelSubscriptionModelTest(TestCase):
    def setUp(self):
        self.a = User.objects.create_user(username='alice', password='pw')
        self.b = User.objects.create_user(username='bob', password='pw')

    def test_defaults(self):
        sub = ChannelSubscription.objects.create(subscriber=self.a, subscribed_to=self.b)
        self.assertTrue(sub.notifications_enabled)
        self.assertFalse(sub.is_favorite)
        self.assertIsNone(sub.deleted_at)
        self.assertIsNotNone(sub.created_at)

    def test_no_self_subscription_check_constraint(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChannelSubscription.objects.create(subscriber=self.a, subscribed_to=self.a)

    def test_model_state_declares_one_active_subscription_constraint(self):
        constraint = next(
            constraint
            for constraint in ChannelSubscription._meta.constraints
            if constraint.name == 'uniq_active_channel_subscription'
        )
        self.assertEqual(constraint.fields, ('subscriber', 'subscribed_to'))
        self.assertEqual(constraint.condition, models.Q(deleted_at__isnull=True))

    def test_only_one_active_subscription_per_user_pair(self):
        first = ChannelSubscription.objects.create(
            subscriber=self.a,
            subscribed_to=self.b,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChannelSubscription.objects.create(
                    subscriber=self.a,
                    subscribed_to=self.b,
                )

        first.deleted_at = timezone.now()
        first.save(update_fields=['deleted_at', 'updated_at'])
        ChannelSubscription.objects.create(
            subscriber=self.a,
            subscribed_to=self.b,
        )
