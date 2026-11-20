"""API regressions for visibility, revision polling, and account lifecycle."""

import json
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.messaging.models import (
    MessagingConversation,
    MessagingMessage,
    MessagingParticipant,
)
from w_craft_back.profile.models import UserProfile
from w_craft_back.profile.services import close_user_account


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class MessagingApiTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.alice = User.objects.create_user('alice', password='password')
        self.bob = User.objects.create_user('bob', password='password')
        self.other = User.objects.create_user('other', password='password')
        self.clients = {}
        for user in (self.alice, self.bob, self.other):
            client = APIClient()
            token = str(UserKey.objects.create(user=user).key)
            client.credentials(HTTP_X_USER_TOKEN=token)
            self.clients[user.pk] = client
        self.client = self.clients[self.alice.pk]
        self.base = '/api/messaging/'

    def tearDown(self) -> None:
        cache.clear()

    def open(self, actor: User | None = None, peer: User | None = None) -> dict:
        response = self.clients[(actor or self.alice).pk].post(
            self.base + 'conversations/',
            {'recipient_id': (peer or self.bob).pk}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def url(self, conversation_id: str, suffix: str = '') -> str:
        return self.base + f'conversations/{conversation_id}/' + suffix

    def send(
        self, conversation_id: str, text: str = 'hello',
        actor: User | None = None, client_id: str | None = None,
    ) -> dict:
        response = self.clients[(actor or self.alice).pk].post(
            self.url(conversation_id, 'messages/'),
            {'text': text, 'client_id': client_id or str(uuid4())}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_header_authentication_required_and_body_token_not_accepted(self) -> None:
        anonymous = APIClient()
        for suffix in ('users/?q=bo', 'conversations/', 'unread/'):
            response = anonymous.get(self.base + suffix)
            self.assertEqual(response.status_code, 401)
            self.assertIn('error', response.json())
        response = anonymous.post(
            self.base + 'conversations/',
            {'recipient_id': self.bob.pk, 'token_user': 'not-a-header'},
            format='json',
        )
        self.assertEqual(response.status_code, 401)

    def test_search_finds_bare_users_and_public_names(self) -> None:
        UserProfile.objects.create(
            user=self.bob, public_username='craft_bob', display_name='Боб Мастер',
            private_account=True, comment_permission='nobody',
            avatar='profiles/bob/avatar.png',
        )
        inactive = User.objects.create_user('inactive_bob', is_active=False)
        for query, expected_id in (
            ('bob', self.bob.pk), ('@craft_bob', self.bob.pk),
            ('Мастер', self.bob.pk), ('other', self.other.pk),
        ):
            response = self.client.get(self.base + 'users/', {'q': query})
            self.assertEqual(response.status_code, 200)
            items = response.json()['items']
            self.assertEqual([item['id'] for item in items], [expected_id])
            self.assertNotIn(inactive.pk, [item['id'] for item in items])
            self.assertEqual(set(items[0]), {
                'id', 'username', 'display_name', 'avatar_url', 'is_active',
            })
        result = self.client.get(self.base + 'users/', {'q': 'bob'}).json()['items'][0]
        self.assertIn('/api/media/', result['avatar_url'])
        self.assertEqual(result['username'], 'craft_bob')
        self.assertFalse(UserProfile.objects.filter(user=self.other).exists())
        self.assertEqual(
            self.client.get(self.base + 'users/?q=alice').json()['items'], [],
        )

    def test_search_and_pagination_validate_edges(self) -> None:
        for q in ('', ' ', '@', 'a'):
            self.assertEqual(
                self.client.get(self.base + 'users/', {'q': q}).json()['items'], [],
            )
        for params in (
            {'q': 'x' * 101}, {'limit': 101}, {'offset': -1}, {'limit': 'bad'},
        ):
            response = self.client.get(self.base + 'users/', params)
            self.assertEqual(response.status_code, 400)
            self.assertIn('error', response.json())
        User.objects.create_user('other_2')
        data = self.client.get(
            self.base + 'users/', {'q': 'other', 'limit': 1, 'offset': 1},
        ).json()
        self.assertEqual(data['total'], 2)
        self.assertEqual(len(data['items']), 1)

    def test_unique_pair_starts_with_recipient_hidden(self) -> None:
        opened = self.open()
        self.assertFalse(opened['is_hidden'])
        self.assertIsNone(opened['last_message'])
        self.assertEqual(self.open()['id'], opened['id'])
        self.assertEqual(self.open(self.bob, self.alice)['id'], opened['id'])
        self.assertEqual(MessagingConversation.objects.count(), 1)
        self.assertEqual(MessagingParticipant.objects.count(), 2)
        recipient = self.clients[self.bob.pk].get(self.base + 'conversations/').json()
        self.assertEqual(recipient['items'], [])
        self.send(opened['id'])
        self.assertEqual(
            self.clients[self.bob.pk].get(
                self.base + 'conversations/',
            ).json()['total'], 1,
        )

    def test_self_missing_and_inactive_recipients_rejected(self) -> None:
        self.bob.is_active = False
        self.bob.save(update_fields=['is_active'])
        for recipient, expected in (
            (self.alice.pk, 400), (999999, 404), (self.bob.pk, 404),
        ):
            response = self.client.post(
                self.base + 'conversations/',
                {'recipient_id': recipient}, format='json',
            )
            self.assertEqual(response.status_code, expected)
        self.assertFalse(MessagingConversation.objects.exists())

    def test_nonparticipant_cannot_discover_or_mutate_conversation(self) -> None:
        conversation_id = self.open()['id']
        message = self.send(conversation_id)['message']
        other = self.clients[self.other.pk]
        for method, suffix, payload in (
            ('get', '', None), ('delete', '', None),
            ('get', 'messages/', None), ('get', 'changes/', None),
            ('post', 'messages/', {'text': 'attack', 'client_id': str(uuid4())}),
            ('post', 'read/', {'through_sequence': 1}),
            ('patch', f'messages/{message["id"]}/',
             {'text': 'attack', 'expected_version': 1}),
            ('delete', f'messages/{message["id"]}/', None),
        ):
            with self.subTest(method=method, suffix=suffix):
                response = getattr(other, method)(
                    self.url(conversation_id, suffix), payload, format='json',
                )
                self.assertEqual(response.status_code, 404, response.content)

    def test_text_is_normalized_unicode_limited_and_nonblank(self) -> None:
        conversation_id = self.open()['id']
        text = '🚀' * 4000
        self.assertEqual(self.send(conversation_id, text)['message']['text'], text)
        self.assertEqual(
            self.send(conversation_id, '  Привет\n  ')['message']['text'], 'Привет',
        )
        for bad in ('', ' \n ', '🚀' * 4001, '\x00'):
            response = self.client.post(
                self.url(conversation_id, 'messages/'),
                {'text': bad, 'client_id': str(uuid4())}, format='json',
            )
            self.assertEqual(response.status_code, 400)
        invalid_unicode = self.client.generic(
            'POST', self.url(conversation_id, 'messages/'),
            json.dumps({'text': '\ud800', 'client_id': str(uuid4())}),
            content_type='application/json',
        )
        self.assertEqual(invalid_unicode.status_code, 400)
        for payload in ({'text': 'hello'}, {'text': 'hello', 'client_id': 'bad'}):
            self.assertEqual(self.client.post(
                self.url(conversation_id, 'messages/'), payload, format='json',
            ).status_code, 400)

    def test_send_retry_after_edit_delete_and_clear(self) -> None:
        conversation_id = self.open()['id']
        client_id = str(uuid4())
        first = self.send(conversation_id, ' original ', client_id=client_id)['message']
        again = self.send(conversation_id, 'original', client_id=client_id)['message']
        self.assertEqual(first['id'], again['id'])
        detail = self.url(conversation_id, f'messages/{first["id"]}/')
        edit = self.client.patch(
            detail, {'text': 'edited', 'expected_version': first['version']},
            format='json',
        )
        self.assertEqual(edit.status_code, 200)
        retry = self.send(conversation_id, 'original', client_id=client_id)['message']
        self.assertEqual(retry['text'], 'edited')
        conflict = self.client.post(
            self.url(conversation_id, 'messages/'),
            {'text': 'different', 'client_id': client_id}, format='json',
        )
        self.assertEqual(conflict.status_code, 409)
        self.client.delete(detail)
        retry = self.send(conversation_id, 'original', client_id=client_id)['message']
        self.assertEqual(retry['text'], '')
        self.assertIsNotNone(retry['deleted_at'])
        self.client.delete(self.url(conversation_id))
        retry = self.client.post(
            self.url(conversation_id, 'messages/'),
            {'text': 'original', 'client_id': client_id}, format='json',
        )
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(retry.json()['error']['code'], 'MESSAGE_CLEARED')
        self.assertTrue(self.client.get(
            self.url(conversation_id),
        ).json()['is_hidden'])
        self.assertEqual(MessagingMessage.objects.count(), 1)

    def test_edit_checks_author_version_and_does_not_create_unread(self) -> None:
        conversation_id = self.open()['id']
        first = self.send(conversation_id)['message']
        bob = self.clients[self.bob.pk]
        bob.post(
            self.url(conversation_id, 'read/'), {'through_sequence': 1},
            format='json',
        )
        detail = self.url(conversation_id, f'messages/{first["id"]}/')
        self.assertEqual(bob.patch(detail, {
            'text': 'not mine', 'expected_version': first['version'],
        }, format='json').status_code, 403)
        self.assertEqual(bob.delete(detail).status_code, 403)
        edit = self.client.patch(detail, {
            'text': 'edited', 'expected_version': first['version'],
        }, format='json')
        self.assertEqual(edit.status_code, 200)
        edited = edit.json()['message']
        self.assertGreater(edited['version'], first['version'])
        self.assertIsNotNone(edited['edited_at'])
        self.assertEqual(self.client.patch(detail, {
            'text': 'stale', 'expected_version': first['version'],
        }, format='json').status_code, 409)
        self.assertEqual(bob.get(self.base + 'unread/').json()['unread_count'], 0)
        changes = bob.get(self.url(conversation_id, 'changes/'), {
            'after_revision': first['version'],
        }).json()
        self.assertEqual(changes['items'][0]['text'], 'edited')

    def test_delete_returns_idempotent_tombstone_and_updates_preview(self) -> None:
        conversation_id = self.open()['id']
        first = self.send(conversation_id, 'keep')['message']
        last = self.send(conversation_id, 'erase')['message']
        detail = self.url(conversation_id, f'messages/{last["id"]}/')
        deleted = self.client.delete(detail).json()
        self.assertEqual(deleted['message']['text'], '')
        self.assertEqual(deleted['conversation']['last_message']['id'], first['id'])
        self.assertEqual(self.client.delete(detail).json(), deleted)
        self.assertEqual(
            self.clients[self.bob.pk].get(
                self.base + 'unread/',
            ).json()['unread_count'], 1,
        )
        self.assertEqual(MessagingMessage.objects.get(pk=last['id']).text, '')
        self.assertEqual(self.client.patch(detail, {
            'text': 'restore', 'expected_version': deleted['message']['version'],
        }, format='json').status_code, 409)
        changes = self.client.get(self.url(conversation_id, 'changes/'), {
            'after_revision': last['version'],
        }).json()
        self.assertEqual(
            changes['items'][0]['deleted_at'], deleted['message']['deleted_at'],
        )

    def test_personal_clear_keeps_peer_history_and_only_new_send_reopens(self) -> None:
        conversation_id = self.open()['id']
        alice_message = self.send(conversation_id, 'alice history')['message']
        bob_message = self.send(
            conversation_id, 'bob history', actor=self.bob,
        )['message']
        cleared = self.client.delete(self.url(conversation_id)).json()
        self.assertTrue(cleared['is_hidden'])
        self.assertEqual(cleared['cleared_through'], 2)
        self.assertEqual(cleared['read_through'], 0)
        self.assertEqual(cleared['unread_count'], 0)
        self.assertIsNone(cleared['last_message'])
        self.assertEqual(self.client.get(
            self.base + 'conversations/',
        ).json()['items'], [])
        self.assertTrue(self.open()['is_hidden'])
        for suffix in ('messages/', 'changes/'):
            self.assertEqual(self.client.get(
                self.url(conversation_id, suffix),
            ).json()['items'], [])
        self.assertEqual(self.client.delete(self.url(
            conversation_id, f'messages/{alice_message["id"]}/',
        )).status_code, 404)
        bob = self.clients[self.bob.pk]
        self.assertEqual(len(bob.get(
            self.url(conversation_id, 'messages/'),
        ).json()['items']), 2)
        self.assertEqual(bob.get(
            self.url(conversation_id),
        ).json()['peer_read_through'], 0)
        bob.patch(self.url(conversation_id, f'messages/{bob_message["id"]}/'), {
            'text': 'edited hidden history', 'expected_version': bob_message['version'],
        }, format='json')
        self.assertTrue(self.client.get(self.url(conversation_id)).json()['is_hidden'])
        self.send(conversation_id, 'new history', actor=self.bob)
        data = self.client.get(self.url(conversation_id, 'messages/')).json()
        self.assertEqual([row['text'] for row in data['items']], ['new history'])
        self.assertFalse(data['conversation']['is_hidden'])
        self.assertEqual(data['conversation']['unread_count'], 1)

    def test_read_is_monotonic_and_rejects_future_or_cleared_boundaries(self) -> None:
        conversation_id = self.open()['id']
        self.send(conversation_id, actor=self.bob)
        second = self.send(conversation_id, actor=self.bob)['message']
        read_url = self.url(conversation_id, 'read/')
        self.assertEqual(self.client.post(read_url, {
            'through_sequence': 3,
        }, format='json').status_code, 400)
        first_read = self.client.post(read_url, {
            'through_sequence': 1,
        }, format='json').json()
        self.assertEqual(first_read['unread_count'], 1)
        stale = self.client.post(
            read_url, {'through_sequence': 0}, format='json',
        ).json()
        self.assertEqual(stale['read_through'], 1)
        self.assertEqual(stale['revision'], first_read['revision'])
        self.client.delete(self.url(conversation_id))
        self.assertEqual(self.client.post(read_url, {
            'through_sequence': second['sequence'],
        }, format='json').status_code, 400)
        peer_state = self.clients[self.bob.pk].get(self.url(conversation_id)).json()
        self.assertEqual(peer_state['peer_read_through'], 1)

    def test_history_pages_and_changes_include_old_updates(self) -> None:
        conversation_id = self.open()['id']
        messages = [self.send(conversation_id, str(i))['message'] for i in range(5)]
        history = self.client.get(
            self.url(conversation_id, 'messages/'), {'limit': 2},
        ).json()
        self.assertEqual([row['sequence'] for row in history['items']], [4, 5])
        self.assertEqual(history['next_before'], 4)
        earlier = self.client.get(self.url(conversation_id, 'messages/'), {
            'before': history['next_before'], 'limit': 2,
        }).json()
        self.assertEqual([row['sequence'] for row in earlier['items']], [2, 3])
        self.client.patch(self.url(conversation_id, f'messages/{messages[0]["id"]}/'), {
            'text': 'old edited', 'expected_version': messages[0]['version'],
        }, format='json')
        self.client.delete(self.url(conversation_id, f'messages/{messages[1]["id"]}/'))
        after = history['sync_revision']
        first = self.client.get(self.url(conversation_id, 'changes/'), {
            'after_revision': after, 'limit': 1,
        }).json()
        self.assertTrue(first['has_more'])
        self.assertEqual(first['items'][0]['text'], 'old edited')
        last = self.client.get(self.url(conversation_id, 'changes/'), {
            'after_revision': first['next_revision'], 'limit': 1,
        }).json()
        self.assertFalse(last['has_more'])
        self.assertEqual(last['items'][0]['text'], '')
        self.assertEqual(last['next_revision'], last['conversation']['revision'])
        self.assertEqual(self.client.get(self.url(conversation_id, 'changes/'), {
            'after_revision': last['next_revision'] + 1,
        }).status_code, 400)

    def test_unread_total_excludes_own_and_cleared_messages(self) -> None:
        first = self.open()['id']
        second = self.open(peer=self.other)['id']
        self.send(first)
        self.send(first, actor=self.bob)
        self.send(second, actor=self.other)
        self.assertEqual(self.client.get(
            self.base + 'unread/',
        ).json()['unread_count'], 2)
        listing = self.client.get(self.base + 'conversations/', {'limit': 1}).json()
        self.assertEqual(listing['total'], 2)
        self.assertEqual(listing['unread_count'], 2)
        self.assertEqual(listing['items'][0]['id'], second)
        self.client.delete(self.url(first))
        self.assertEqual(self.client.get(
            self.base + 'unread/',
        ).json()['unread_count'], 1)

    def test_account_closure_delivers_every_tombstone_in_pages(self) -> None:
        conversation_id = self.open()['id']
        self.send(conversation_id, 'surviving text')
        authored = [
            self.send(conversation_id, f'private {i}', actor=self.bob)['message']
            for i in range(5)
        ]
        cursor = authored[-1]['version']
        close_user_account(self.bob, 'password')
        rows = []
        while True:
            page = self.client.get(self.url(conversation_id, 'changes/'), {
                'after_revision': cursor, 'limit': 2,
            }).json()
            rows.extend(page['items'])
            self.assertGreater(page['next_revision'], cursor)
            cursor = page['next_revision']
            if not page['has_more']:
                break
        self.assertEqual({row['id'] for row in rows}, {row['id'] for row in authored})
        self.assertEqual(len({row['version'] for row in rows}), len(rows))
        self.assertTrue(all(row['text'] == '' and row['deleted_at'] for row in rows))
        self.assertFalse(MessagingMessage.objects.filter(
            sender=self.bob,
        ).exclude(text='').exists())
        data = self.client.get(self.url(conversation_id)).json()
        self.assertFalse(data['can_send'])
        self.assertEqual(data['peer'], {
            'id': self.bob.pk, 'username': '', 'display_name': '',
            'avatar_url': None, 'is_active': False,
        })
        self.assertEqual(data['last_message']['text'], 'surviving text')
        self.assertEqual(data['unread_count'], 0)
        self.assertEqual(self.client.post(self.url(conversation_id, 'messages/'), {
            'text': 'unavailable', 'client_id': str(uuid4()),
        }, format='json').status_code, 409)
        self.assertEqual(self.clients[self.bob.pk].get(
            self.base + 'conversations/',
        ).status_code, 401)
