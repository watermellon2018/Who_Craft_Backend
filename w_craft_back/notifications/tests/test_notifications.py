from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.notifications.models import EmailNotificationDelivery, Notification
from w_craft_back.notifications.services import NotificationEvent, dispatch_notification
from w_craft_back.profile.models import UserProfile


class NotificationDispatcherTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='recipient',
            email='recipient@example.test',
        )
        self.profile = UserProfile.objects.create(
            user=self.user,
            notifications_in_app=True,
            notifications_email=True,
        )

    @patch('w_craft_back.notifications.services.send_notification_email')
    def test_dispatches_enabled_channels_after_commit_and_is_idempotent(self, send_email):
        event = NotificationEvent(
            recipient=self.user,
            type=Notification.Type.SYSTEM,
            title='Title',
            message='Message',
            target_url='/projects',
            idempotency_key='event:1',
        )
        with self.captureOnCommitCallbacks(execute=True):
            first = dispatch_notification(event)
        with self.captureOnCommitCallbacks(execute=True):
            second = dispatch_notification(event)

        self.assertIsNotNone(first.notification)
        self.assertEqual(second.notification.id, first.notification.id)
        self.assertEqual(Notification.objects.count(), 1)
        delivery = EmailNotificationDelivery.objects.get()
        self.assertEqual(delivery.status, EmailNotificationDelivery.Status.SENT)
        self.assertEqual(delivery.attempts, 1)
        send_email.assert_called_once_with(
            recipient_email='recipient@example.test',
            title='Title',
            message='Message',
        )

    def test_respects_disabled_channels(self):
        self.profile.notifications_in_app = False
        self.profile.notifications_email = False
        self.profile.save(update_fields=['notifications_in_app', 'notifications_email'])
        result = dispatch_notification(NotificationEvent(
            recipient=self.user,
            type=Notification.Type.SYSTEM,
            title='Hidden',
        ))
        self.assertIsNone(result.notification)
        self.assertFalse(result.email_scheduled)
        self.assertFalse(EmailNotificationDelivery.objects.exists())

    @patch('w_craft_back.notifications.services.send_notification_email')
    def test_failed_delivery_is_durable_and_retry_command_sends_it(self, send_email):
        send_email.side_effect = OSError('smtp unavailable')
        event = NotificationEvent(
            recipient=self.user,
            type=Notification.Type.SYSTEM,
            title='Retry me',
            message='Durable body',
            idempotency_key='event:retry',
        )
        with self.captureOnCommitCallbacks(execute=True):
            dispatch_notification(event)

        delivery = EmailNotificationDelivery.objects.get()
        self.assertEqual(delivery.status, EmailNotificationDelivery.Status.FAILED)
        self.assertEqual(delivery.attempts, 1)
        self.assertIn('smtp unavailable', delivery.last_error)

        send_email.side_effect = None
        call_command('retry_notification_emails', limit=10, verbosity=0)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailNotificationDelivery.Status.SENT)
        self.assertEqual(delivery.attempts, 2)
        self.assertIsNotNone(delivery.sent_at)
        self.assertEqual(send_email.call_count, 2)

    @patch('w_craft_back.notifications.services.send_notification_email')
    def test_duplicate_failed_event_replays_retry_but_sent_event_does_not(self, send_email):
        event = NotificationEvent(
            recipient=self.user,
            type=Notification.Type.SYSTEM,
            title='Replay',
            idempotency_key='event:replay',
        )
        send_email.side_effect = OSError('first failure')
        with self.captureOnCommitCallbacks(execute=True):
            dispatch_notification(event)
        send_email.side_effect = None
        with self.captureOnCommitCallbacks(execute=True):
            replay = dispatch_notification(event)
        with self.captureOnCommitCallbacks(execute=True):
            sent_replay = dispatch_notification(event)

        delivery = EmailNotificationDelivery.objects.get()
        self.assertEqual(delivery.status, EmailNotificationDelivery.Status.SENT)
        self.assertTrue(replay.email_scheduled)
        self.assertFalse(sent_replay.email_scheduled)
        self.assertEqual(delivery.attempts, 2)
        self.assertEqual(send_email.call_count, 2)

    def test_rejects_external_target_url(self):
        with self.assertRaises(ValueError):
            dispatch_notification(NotificationEvent(
                recipient=self.user,
                type=Notification.Type.SYSTEM,
                title='Unsafe',
                target_url='https://example.test/phish',
            ))


class NotificationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='reader')
        key = UserKey.objects.create(user=self.user)
        self.headers = {'HTTP_X_USER_TOKEN': key.key}
        self.first = Notification.objects.create(
            recipient=self.user,
            type=Notification.Type.SYSTEM,
            title='First',
        )
        self.second = Notification.objects.create(
            recipient=self.user,
            type=Notification.Type.SYSTEM,
            title='Second',
        )

    def test_list_returns_results_and_unread_count(self):
        response = self.client.get(reverse('notification-list'), **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['unread_count'], 2)
        self.assertEqual(len(response.json()['results']), 2)

    def test_read_one_and_read_all_are_recipient_scoped(self):
        other = User.objects.create_user(username='other-reader')
        other_notification = Notification.objects.create(
            recipient=other,
            type=Notification.Type.SYSTEM,
            title='Other',
        )
        response = self.client.post(
            reverse('notification-read', args=[self.first.id]),
            **self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.first.refresh_from_db()
        self.assertTrue(self.first.is_read)

        response = self.client.post(reverse('notification-read-all'), **self.headers)
        self.assertEqual(response.json(), {'unread_count': 0, 'updated': 1})
        other_notification.refresh_from_db()
        self.assertFalse(other_notification.is_read)
