from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.profile.models import UserProfile
from w_craft_back.subscriptions import services


def _auth_client(token: str) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_X_USER_TOKEN=token)
    return client


class SubscriptionsViewsBase(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw')
        self.bob = User.objects.create_user(username='bob', password='pw')
        UserProfile.objects.create(user=self.alice)
        UserProfile.objects.create(user=self.bob)
        self.alice_key = UserKey.objects.create(user=self.alice)
        self.bob_key = UserKey.objects.create(user=self.bob)
        self.token = str(self.alice_key.key)
        self.client = _auth_client(self.token)


class MySubscriptionsViewTest(SubscriptionsViewsBase):
    def test_unauthorized_without_token(self):
        anon = APIClient()
        response = anon.get('/api/subscriptions/')
        self.assertEqual(response.status_code, 401)

    def test_returns_empty_when_no_subscriptions(self):
        response = self.client.get('/api/subscriptions/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['items'], [])
        self.assertEqual(data['total'], 0)
        self.assertEqual(data['favoriteCount'], 0)

    def test_returns_subscriptions(self):
        services.subscribe(self.alice, self.bob.id)
        response = self.client.get('/api/subscriptions/')
        data = response.json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['id'], self.bob.id)

    def test_invalid_pagination_returns_400(self):
        response = self.client.get('/api/subscriptions/?limit=not-a-number')
        self.assertEqual(response.status_code, 400)


class ChannelSearchViewTest(SubscriptionsViewsBase):
    def test_unauthorized_without_token(self):
        anon = APIClient()
        response = anon.get('/api/channels/search/?q=bob')
        self.assertEqual(response.status_code, 401)

    def test_empty_query_returns_empty_items(self):
        response = self.client.get('/api/channels/search/?q=')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['items'], [])

    def test_invalid_pagination_returns_400(self):
        response = self.client.get('/api/channels/search/?q=bob&limit=-5')
        self.assertEqual(response.status_code, 400)


class ChannelSubscribeViewTest(SubscriptionsViewsBase):
    def test_unauthorized_without_token(self):
        anon = APIClient()
        response = anon.post(f'/api/channels/{self.bob.id}/subscribe/')
        self.assertEqual(response.status_code, 401)

    def test_subscribe_happy_path(self):
        response = self.client.post(f'/api/channels/{self.bob.id}/subscribe/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['subscription']['isSubscribed'])
        self.assertEqual(payload['subscription']['targetUserId'], self.bob.id)

    def test_self_subscribe_returns_400(self):
        response = self.client.post(f'/api/channels/{self.alice.id}/subscribe/')
        self.assertEqual(response.status_code, 400)

    def test_missing_target_returns_404(self):
        response = self.client.post('/api/channels/99999/subscribe/')
        self.assertEqual(response.status_code, 404)

    def test_unsubscribe_happy_path(self):
        services.subscribe(self.alice, self.bob.id)
        response = self.client.delete(f'/api/channels/{self.bob.id}/subscribe/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['subscription']['isSubscribed'])

    def test_unsubscribe_without_active_returns_404(self):
        response = self.client.delete(f'/api/channels/{self.bob.id}/subscribe/')
        self.assertEqual(response.status_code, 404)

    def test_self_unsubscribe_returns_400(self):
        response = self.client.delete(f'/api/channels/{self.alice.id}/subscribe/')
        self.assertEqual(response.status_code, 400)


class ChannelSubscriptionSettingsViewTest(SubscriptionsViewsBase):
    def setUp(self):
        super().setUp()
        services.subscribe(self.alice, self.bob.id)

    def test_unauthorized_without_token(self):
        anon = APIClient()
        response = anon.patch(
            f'/api/channels/{self.bob.id}/subscription/', {'isFavorite': True}, format='json',
        )
        self.assertEqual(response.status_code, 401)

    def test_updates_is_favorite(self):
        response = self.client.patch(
            f'/api/channels/{self.bob.id}/subscription/', {'isFavorite': True}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['subscription']['isFavorite'])

    def test_updates_notifications_enabled(self):
        response = self.client.patch(
            f'/api/channels/{self.bob.id}/subscription/',
            {'notificationsEnabled': False},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['subscription']['notificationsEnabled'])

    def test_empty_payload_returns_400(self):
        response = self.client.patch(
            f'/api/channels/{self.bob.id}/subscription/', {}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_subscription_returns_404(self):
        services.unsubscribe(self.alice, self.bob.id)
        response = self.client.patch(
            f'/api/channels/{self.bob.id}/subscription/', {'isFavorite': True}, format='json',
        )
        self.assertEqual(response.status_code, 404)


class UserSubscribersViewTest(SubscriptionsViewsBase):
    def test_unauthorized_without_token(self):
        anon = APIClient()
        response = anon.get(f'/api/users/{self.bob.id}/subscribers/')
        self.assertEqual(response.status_code, 401)

    def test_returns_subscribers_list(self):
        services.subscribe(self.alice, self.bob.id)
        response = self.client.get(f'/api/users/{self.bob.id}/subscribers/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['id'], self.alice.id)


class UserSubscriptionsViewTest(SubscriptionsViewsBase):
    def test_unauthorized_without_token(self):
        anon = APIClient()
        response = anon.get(f'/api/users/{self.alice.id}/subscriptions/')
        self.assertEqual(response.status_code, 401)

    def test_returns_subscriptions_list(self):
        services.subscribe(self.alice, self.bob.id)
        response = self.client.get(f'/api/users/{self.alice.id}/subscriptions/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['id'], self.bob.id)
