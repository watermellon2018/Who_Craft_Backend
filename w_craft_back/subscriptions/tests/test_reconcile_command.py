from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from w_craft_back.profile.models import UserProfile
from w_craft_back.subscriptions import services


class ReconcileSubscriptionCountersCommandTest(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        UserProfile.objects.create(user=self.alice)
        UserProfile.objects.create(user=self.bob)

    def test_check_detects_drift_and_apply_repairs_it(self):
        services.subscribe(self.alice, self.bob.id)
        UserProfile.objects.filter(user=self.alice).update(subscriptions_count=9)
        UserProfile.objects.filter(user=self.bob).update(subscribers_count=7)

        with self.assertRaises(CommandError):
            call_command(
                'reconcile_subscription_counters',
                '--check',
                stdout=StringIO(),
            )

        self.assertEqual(
            UserProfile.objects.get(user=self.alice).subscriptions_count,
            9,
        )
        self.assertEqual(
            UserProfile.objects.get(user=self.bob).subscribers_count,
            7,
        )

        call_command(
            'reconcile_subscription_counters',
            '--apply',
            stdout=StringIO(),
        )

        alice_profile = UserProfile.objects.get(user=self.alice)
        bob_profile = UserProfile.objects.get(user=self.bob)
        self.assertEqual(alice_profile.subscriptions_count, 1)
        self.assertEqual(bob_profile.subscribers_count, 1)
        call_command(
            'reconcile_subscription_counters',
            '--check',
            stdout=StringIO(),
        )

    def test_apply_repairs_counters_after_subscription_cascade_delete(self):
        services.subscribe(self.alice, self.bob.id)
        bob_id = self.bob.id
        self.bob.delete()

        call_command(
            'reconcile_subscription_counters',
            '--apply',
            stdout=StringIO(),
        )

        alice_profile = UserProfile.objects.get(user=self.alice)
        self.assertEqual(alice_profile.subscriptions_count, 0)
        self.assertFalse(UserProfile.objects.filter(user_id=bob_id).exists())

    def test_apply_repairs_subscriber_counter_after_subscriber_delete(self):
        services.subscribe(self.alice, self.bob.id)
        self.alice.delete()

        call_command(
            'reconcile_subscription_counters',
            '--apply',
            stdout=StringIO(),
        )

        bob_profile = UserProfile.objects.get(user=self.bob)
        self.assertEqual(bob_profile.subscribers_count, 0)

    def test_apply_creates_missing_profile_for_active_participant(self):
        services.subscribe(self.alice, self.bob.id)
        UserProfile.objects.filter(user=self.alice).delete()

        call_command(
            'reconcile_subscription_counters',
            '--apply',
            stdout=StringIO(),
        )

        alice_profile = UserProfile.objects.get(user=self.alice)
        self.assertEqual(alice_profile.subscriptions_count, 1)
