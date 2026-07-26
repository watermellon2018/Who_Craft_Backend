from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase

from w_craft_back.profile.models import UserProfile
from w_craft_back.subscriptions import services
from w_craft_back.subscriptions.models import ChannelSubscription


def _refresh_profile(user: User) -> UserProfile:
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.refresh_from_db()
    return profile


class SubscribeServiceTest(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        UserProfile.objects.create(user=self.alice)
        UserProfile.objects.create(user=self.bob)

    def test_subscribe_creates_subscription_and_increments_counters(self):
        state = services.subscribe(self.alice, self.bob.id)

        self.assertTrue(state.is_subscribed)
        self.assertFalse(state.is_favorite)
        self.assertTrue(state.notifications_enabled)
        self.assertEqual(state.target_user_id, self.bob.id)

        self.assertEqual(_refresh_profile(self.bob).subscribers_count, 1)
        self.assertEqual(_refresh_profile(self.alice).subscriptions_count, 1)

        sub = ChannelSubscription.objects.get(subscriber=self.alice, subscribed_to=self.bob)
        self.assertIsNone(sub.deleted_at)

    def test_subscribe_is_idempotent(self):
        services.subscribe(self.alice, self.bob.id)
        services.subscribe(self.alice, self.bob.id)

        # Counters must not double-increment on repeated subscribe.
        self.assertEqual(_refresh_profile(self.bob).subscribers_count, 1)
        self.assertEqual(_refresh_profile(self.alice).subscriptions_count, 1)
        self.assertEqual(
            ChannelSubscription.objects.filter(
                subscriber=self.alice, subscribed_to=self.bob, deleted_at__isnull=True,
            ).count(),
            1,
        )

    def test_resubscribe_after_unsubscribe_restores_active_row(self):
        services.subscribe(self.alice, self.bob.id)
        services.unsubscribe(self.alice, self.bob.id)
        self.assertEqual(_refresh_profile(self.bob).subscribers_count, 0)

        state = services.subscribe(self.alice, self.bob.id)
        self.assertTrue(state.is_subscribed)
        self.assertEqual(_refresh_profile(self.bob).subscribers_count, 1)
        self.assertEqual(_refresh_profile(self.alice).subscriptions_count, 1)

    def test_self_subscription_raises(self):
        with self.assertRaises(services.SelfSubscriptionError):
            services.subscribe(self.alice, self.alice.id)

    def test_subscribe_to_missing_user_raises(self):
        with self.assertRaises(services.TargetNotFoundError):
            services.subscribe(self.alice, 99999)

    def test_subscribe_to_inactive_user_raises(self):
        self.bob.is_active = False
        self.bob.save(update_fields=['is_active'])
        with self.assertRaises(services.TargetNotFoundError):
            services.subscribe(self.alice, self.bob.id)


class UnsubscribeServiceTest(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        UserProfile.objects.create(user=self.alice)
        UserProfile.objects.create(user=self.bob)
        services.subscribe(self.alice, self.bob.id)

    def test_unsubscribe_soft_deletes_and_resets_flags(self):
        services.update_settings(self.alice, self.bob.id, is_favorite=True, notifications_enabled=True)
        state = services.unsubscribe(self.alice, self.bob.id)

        self.assertFalse(state.is_subscribed)
        self.assertFalse(state.is_favorite)
        self.assertFalse(state.notifications_enabled)

        sub = ChannelSubscription.objects.get(subscriber=self.alice, subscribed_to=self.bob)
        self.assertIsNotNone(sub.deleted_at)
        self.assertFalse(sub.is_favorite)
        self.assertFalse(sub.notifications_enabled)

        self.assertEqual(_refresh_profile(self.bob).subscribers_count, 0)
        self.assertEqual(_refresh_profile(self.alice).subscriptions_count, 0)

    def test_unsubscribe_self_raises(self):
        with self.assertRaises(services.SelfSubscriptionError):
            services.unsubscribe(self.alice, self.alice.id)

    def test_unsubscribe_without_active_subscription_raises(self):
        services.unsubscribe(self.alice, self.bob.id)
        with self.assertRaises(services.SubscriptionNotFoundError):
            services.unsubscribe(self.alice, self.bob.id)

    def test_counters_do_not_go_below_zero(self):
        # Force counters to 0, then unsubscribe — Greatest(... - 1, 0) must clamp.
        UserProfile.objects.filter(user=self.bob).update(subscribers_count=0)
        UserProfile.objects.filter(user=self.alice).update(subscriptions_count=0)
        services.unsubscribe(self.alice, self.bob.id)
        self.assertEqual(_refresh_profile(self.bob).subscribers_count, 0)
        self.assertEqual(_refresh_profile(self.alice).subscriptions_count, 0)


class UpdateSettingsServiceTest(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        UserProfile.objects.create(user=self.alice)
        UserProfile.objects.create(user=self.bob)
        services.subscribe(self.alice, self.bob.id)

    def test_updates_only_provided_fields(self):
        state = services.update_settings(self.alice, self.bob.id, is_favorite=True)
        self.assertTrue(state.is_favorite)
        self.assertTrue(state.notifications_enabled)  # unchanged from default

        state2 = services.update_settings(self.alice, self.bob.id, notifications_enabled=False)
        self.assertTrue(state2.is_favorite)
        self.assertFalse(state2.notifications_enabled)

    def test_none_values_do_not_overwrite(self):
        services.update_settings(self.alice, self.bob.id, is_favorite=True, notifications_enabled=False)
        state = services.update_settings(self.alice, self.bob.id, is_favorite=None, notifications_enabled=None)
        self.assertTrue(state.is_favorite)
        self.assertFalse(state.notifications_enabled)

    def test_missing_subscription_raises(self):
        services.unsubscribe(self.alice, self.bob.id)
        with self.assertRaises(services.SubscriptionNotFoundError):
            services.update_settings(self.alice, self.bob.id, is_favorite=True)


class ListMySubscriptionsServiceTest(TestCase):
    def setUp(self):
        self.me = User.objects.create_user(username='me', password='pw')
        UserProfile.objects.create(user=self.me)
        self.targets = []
        for i in range(3):
            user = User.objects.create_user(username=f'target{i}', password='pw')
            UserProfile.objects.create(user=user, display_name=f'Target {i}', public_username=f'target{i}')
            self.targets.append(user)
            services.subscribe(self.me, user.id)
        # Mark the last subscription as favorite — it should be returned first.
        services.update_settings(self.me, self.targets[-1].id, is_favorite=True)

    def test_favorites_come_first_then_newest_created(self):
        data = services.list_my_subscriptions(self.me, limit=20, offset=0)
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['favoriteCount'], 1)
        # favorites first
        self.assertTrue(data['items'][0]['isFavorite'])
        # remaining order is newest-first by created_at
        non_favorites = [item['id'] for item in data['items'][1:]]
        # targets[1] subscribed after targets[0], so targets[1] is newer
        self.assertEqual(non_favorites, [self.targets[1].id, self.targets[0].id])

    def test_inactive_targets_are_excluded(self):
        self.targets[0].is_active = False
        self.targets[0].save(update_fields=['is_active'])
        data = services.list_my_subscriptions(self.me, limit=20, offset=0)
        self.assertEqual(data['total'], 2)

    def test_soft_deleted_subscriptions_excluded(self):
        services.unsubscribe(self.me, self.targets[0].id)
        data = services.list_my_subscriptions(self.me, limit=20, offset=0)
        self.assertEqual(data['total'], 2)
        self.assertNotIn(self.targets[0].id, [item['id'] for item in data['items']])

    def test_pagination_clamps_limit_and_offset(self):
        data = services.list_my_subscriptions(self.me, limit=99999, offset=-5)
        self.assertEqual(data['limit'], services._MAX_PAGE_LIMIT)
        self.assertEqual(data['offset'], 0)

    def test_display_name_falls_back_to_username(self):
        # Profile.display_name is set in setUp; clear it for one target and re-check.
        UserProfile.objects.filter(user=self.targets[0]).update(display_name='')
        data = services.list_my_subscriptions(self.me, limit=20, offset=0)
        row = next(item for item in data['items'] if item['id'] == self.targets[0].id)
        self.assertEqual(row['displayName'], self.targets[0].username)


class NormalizeQueryTest(TestCase):
    def test_strips_at_prefix_and_lowercases(self):
        self.assertEqual(services._normalize_query('@FooBar'), 'foobar')

    def test_trims_whitespace(self):
        self.assertEqual(services._normalize_query('   spaced   '), 'spaced')

    def test_handles_none(self):
        self.assertEqual(services._normalize_query(None), '')


class SearchChannelsServiceTest(TestCase):
    """Requires PostgreSQL with pg_trgm extension."""

    def setUp(self):
        self.me = User.objects.create_user(username='me', password='pw')
        UserProfile.objects.create(user=self.me)

        self.alice = User.objects.create_user(username='alice_raw', password='pw')
        UserProfile.objects.create(
            user=self.alice,
            public_username='alice',
            display_name='Alice Wonderland',
        )

        self.bob = User.objects.create_user(username='bob_raw', password='pw')
        UserProfile.objects.create(
            user=self.bob,
            public_username='bobby',
            display_name='Bobby Brown',
        )

    def test_empty_query_returns_empty_items(self):
        data = services.search_channels(self.me, '', limit=20, offset=0)
        self.assertEqual(data['items'], [])
        self.assertEqual(data['total'], 0)

    def test_finds_users_by_public_username(self):
        if connection.vendor != 'postgresql':
            self.skipTest('search_channels uses pg_trgm and ILIKE — requires PostgreSQL')
        data = services.search_channels(self.me, 'alice', limit=20, offset=0)
        ids = [item['id'] for item in data['items']]
        self.assertIn(self.alice.id, ids)
        self.assertNotIn(self.me.id, ids)

    def test_excludes_inactive_users(self):
        if connection.vendor != 'postgresql':
            self.skipTest('search_channels uses pg_trgm and ILIKE — requires PostgreSQL')
        self.alice.is_active = False
        self.alice.save(update_fields=['is_active'])
        data = services.search_channels(self.me, 'alice', limit=20, offset=0)
        self.assertNotIn(self.alice.id, [item['id'] for item in data['items']])

    def test_escape_wildcards_in_query(self):
        if connection.vendor != 'postgresql':
            self.skipTest('search_channels uses pg_trgm and ILIKE — requires PostgreSQL')
        # Raw % should be treated literally, not as a wildcard match.
        data = services.search_channels(self.me, '%', limit=20, offset=0)
        # No public_username contains a literal '%', so we expect zero matches.
        self.assertEqual(data['items'], [])

    def test_subscription_flags_reflect_existing_state(self):
        if connection.vendor != 'postgresql':
            self.skipTest('search_channels uses pg_trgm and ILIKE — requires PostgreSQL')
        services.subscribe(self.me, self.alice.id)
        services.update_settings(self.me, self.alice.id, is_favorite=True)
        data = services.search_channels(self.me, 'alice', limit=20, offset=0)
        row = next(item for item in data['items'] if item['id'] == self.alice.id)
        self.assertTrue(row['isSubscribed'])
        self.assertTrue(row['isFavorite'])
        self.assertTrue(row['notificationsEnabled'])


class ListSubscribersServiceTest(TestCase):
    def setUp(self):
        self.target = User.objects.create_user(username='target', password='pw')
        UserProfile.objects.create(user=self.target)
        self.subs = []
        for i in range(3):
            user = User.objects.create_user(username=f'sub{i}', password='pw')
            UserProfile.objects.create(user=user)
            services.subscribe(user, self.target.id)
            self.subs.append(user)

    def test_returns_only_active_subscribers(self):
        services.unsubscribe(self.subs[0], self.target.id)
        data = services.list_subscribers(self.target.id, limit=20, offset=0)
        self.assertEqual(data['total'], 2)
        ids = [item['id'] for item in data['items']]
        self.assertNotIn(self.subs[0].id, ids)

    def test_excludes_inactive_subscribers(self):
        self.subs[0].is_active = False
        self.subs[0].save(update_fields=['is_active'])
        data = services.list_subscribers(self.target.id, limit=20, offset=0)
        self.assertEqual(data['total'], 2)


class ListUserSubscriptionsServiceTest(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username='viewer', password='pw')
        UserProfile.objects.create(user=self.viewer)
        for i in range(2):
            target = User.objects.create_user(username=f'target{i}', password='pw')
            UserProfile.objects.create(user=target)
            services.subscribe(self.viewer, target.id)

    def test_returns_user_subscriptions(self):
        data = services.list_user_subscriptions(self.viewer.id, limit=20, offset=0)
        self.assertEqual(data['total'], 2)
