"""Real PostgreSQL transactions exercise ordering and account-close races."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from typing import Callable
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings

from w_craft_back.messaging import services
from w_craft_back.messaging.errors import MessagingError
from w_craft_back.messaging.models import MessagingConversation, MessagingMessage
from w_craft_back.profile.services import close_user_account


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class MessagingConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        self.alice = User.objects.create_user('race_alice', password='password')
        self.bob = User.objects.create_user('race_bob', password='password')

    @staticmethod
    def isolated(action: Callable, barrier: Barrier | None = None) -> object:
        """Give each worker its own connection and bounded database waits."""
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '10s'")
            if barrier is not None:
                barrier.wait(timeout=10)
            try:
                return action()
            except MessagingError as exc:
                return exc
        finally:
            connections.close_all()

    def concurrently(self, *actions: Callable) -> list:
        barrier = Barrier(len(actions))
        with ThreadPoolExecutor(max_workers=len(actions)) as executor:
            futures = [
                executor.submit(self.isolated, action, barrier)
                for action in actions
            ]
            return [future.result(timeout=20) for future in futures]

    def conversation_id(self) -> str:
        return services.open_conversation(self.alice, self.bob.pk)['id']

    def test_opposite_pair_creation_produces_one_conversation(self) -> None:
        results = self.concurrently(
            lambda: services.open_conversation(self.alice, self.bob.pk),
            lambda: services.open_conversation(self.bob, self.alice.pk),
        )
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(MessagingConversation.objects.count(), 1)
        self.assertEqual(MessagingConversation.objects.get().participants.count(), 2)

    def test_duplicate_concurrent_send_allocates_one_sequence(self) -> None:
        conversation_id = self.conversation_id()
        client_id = uuid4()
        results = self.concurrently(*[
            lambda: services.send_message(
                self.alice, conversation_id, 'one send', client_id,
            )
            for _ in range(2)
        ])
        self.assertEqual(results[0]['message']['id'], results[1]['message']['id'])
        self.assertEqual(MessagingMessage.objects.count(), 1)
        conversation = MessagingConversation.objects.get()
        self.assertEqual(conversation.last_sequence, 1)
        self.assertEqual(conversation.revision, 1)

    def test_concurrent_edits_have_one_winner(self) -> None:
        conversation_id = self.conversation_id()
        first = services.send_message(
            self.alice, conversation_id, 'original', uuid4(),
        )['message']
        results = self.concurrently(*[
            lambda text=text: services.edit_message(
                self.alice, conversation_id, first['id'], text, first['version'],
            )
            for text in ('first edit', 'second edit')
        ])
        errors = [result for result in results if isinstance(result, MessagingError)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status_code, 409)
        winner = next(result for result in results if isinstance(result, dict))
        self.assertEqual(MessagingMessage.objects.get().text, winner['message']['text'])

    def test_clear_and_new_send_have_an_atomic_history_boundary(self) -> None:
        conversation_id = self.conversation_id()
        services.send_message(self.bob, conversation_id, 'old', uuid4())
        results = self.concurrently(
            lambda: services.clear_conversation(self.alice, conversation_id),
            lambda: services.send_message(self.bob, conversation_id, 'new', uuid4()),
        )
        cleared = results[0]['cleared_through']
        sent = results[1]['message']['sequence']
        history = services.message_history(self.alice, conversation_id)
        if sent > cleared:
            self.assertEqual([row['text'] for row in history['items']], ['new'])
            self.assertFalse(history['conversation']['is_hidden'])
            self.assertEqual(history['conversation']['unread_count'], 1)
        else:
            self.assertEqual(history['items'], [])
            self.assertTrue(history['conversation']['is_hidden'])
            self.assertEqual(history['conversation']['unread_count'], 0)
        self.assertEqual(history['conversation']['read_through'], 0)

    def test_stale_sender_cannot_write_after_account_closure(self) -> None:
        conversation_id = self.conversation_id()
        services.send_message(self.alice, conversation_id, 'private', uuid4())
        closing = Event()
        send_attempted = Event()
        real_scrub = services.scrub_user_messages

        def gated_scrub(user: User) -> None:
            closing.set()
            if not send_attempted.wait(timeout=10):
                raise AssertionError('The concurrent send did not start.')
            real_scrub(user)

        def stale_send() -> dict:
            if not closing.wait(timeout=10):
                raise AssertionError('Account closure did not start.')
            send_attempted.set()
            return services.send_message(
                self.alice, conversation_id, 'stale write', uuid4(),
            )

        with patch.object(services, 'scrub_user_messages', side_effect=gated_scrub):
            results = self.concurrently(
                lambda: close_user_account(self.alice, 'password'), stale_send,
            )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], MessagingError)
        self.assertEqual(results[1].status_code, 401)
        self.assertFalse(MessagingMessage.objects.exclude(text='').exists())
        self.assertFalse(services.get_conversation(
            self.bob, conversation_id,
        )['can_send'])

    def test_close_recipient_and_send_respect_commit_order(self) -> None:
        conversation_id = self.conversation_id()
        results = self.concurrently(
            lambda: services.send_message(
                self.alice, conversation_id, 'racing message', uuid4(),
            ),
            lambda: close_user_account(self.bob, 'password'),
        )
        self.assertIsNone(results[1])
        if isinstance(results[0], MessagingError):
            self.assertEqual(results[0].status_code, 409)
            self.assertFalse(MessagingMessage.objects.exists())
        else:
            self.assertEqual(MessagingMessage.objects.count(), 1)
        self.assertFalse(services.get_conversation(
            self.alice, conversation_id,
        )['can_send'])

    def test_simultaneous_account_closures_do_not_deadlock(self) -> None:
        conversation_id = self.conversation_id()
        services.send_message(self.alice, conversation_id, 'alice', uuid4())
        services.send_message(self.bob, conversation_id, 'bob', uuid4())
        results = self.concurrently(
            lambda: close_user_account(self.alice, 'password'),
            lambda: close_user_account(self.bob, 'password'),
        )
        self.assertEqual(results, [None, None])
        self.assertFalse(MessagingMessage.objects.exclude(text='').exists())
        versions = list(MessagingMessage.objects.values_list('version', flat=True))
        self.assertEqual(len(set(versions)), 2)
