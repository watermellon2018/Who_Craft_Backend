from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

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
