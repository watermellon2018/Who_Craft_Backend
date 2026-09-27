"""Credential isolation, revocation and payer authorization regressions."""

import json
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.personal_api.access import (
    credential_for_candidate,
    generation_source,
    reject_candidate,
    resolve_generation_access,
    sign_generation_access,
    validate_generation_access,
)
from w_craft_back.personal_api.crypto import decrypt_secret
from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.personal_api.models import (
    PersonalAPIConnection,
    PersonalAPIPreference,
)
from w_craft_back.personal_api.services import (
    create_connection,
    revoke_connection,
    save_preference,
    update_connection,
)
from w_craft_back.profile.services import close_user_account


TEST_KEY = Fernet.generate_key().decode()
FIRST_SECRET = 'fake-personal-provider-key-alice-1234'
SECOND_SECRET = 'fake-personal-provider-key-bob-5678'


@override_settings(
    PERSONAL_API_ENCRYPTION_KEY=TEST_KEY,
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class PersonalAPIBase(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.alice = User.objects.create_user('api-alice', password='password')
        self.bob = User.objects.create_user('api-bob', password='password')
        self.client = APIClient()
        self.client.credentials(HTTP_X_USER_TOKEN=str(
            UserKey.objects.create(user=self.alice).key,
        ))

    def tearDown(self) -> None:
        cache.clear()

    def connection(
        self, user: User | None = None, *, provider: str = 'gemini',
        secret: str = FIRST_SECRET,
    ) -> dict:
        return create_connection(
            user or self.alice, name='My connection', provider=provider, apiKey=secret,
        )

    def configure(
        self, *connections: dict, modality: str = 'image',
        model: str = 'gemini-flash-image', source: str = 'personal',
    ) -> dict:
        return save_preference(
            self.alice, modality, source=source,
            routes=[{'connectionId': row['id'], 'modelKey': model}
                    for row in connections],
        )


class PersonalAPICredentialTests(PersonalAPIBase):
    def test_authentication_required_and_body_token_not_accepted(self) -> None:
        anonymous = APIClient()
        response = anonymous.get('/api/personal-api/settings/')
        self.assertEqual(response.status_code, 401)
        response = anonymous.post('/api/personal-api/connections/', {
            'name': 'test', 'provider': 'gemini', 'apiKey': FIRST_SECRET,
            'token_user': 'body-token',
        }, format='json')
        self.assertEqual(response.status_code, 401)

    def test_create_stores_ciphertext_and_never_returns_secret(self) -> None:
        response = self.client.post('/api/personal-api/connections/', {
            'name': 'Images', 'provider': 'gemini', 'apiKey': FIRST_SECRET,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        row = PersonalAPIConnection.objects.get(pk=response.json()['id'])
        self.assertEqual(decrypt_secret(row), FIRST_SECRET)
        self.assertNotIn(FIRST_SECRET, row.encrypted_key)
        self.assertNotIn(FIRST_SECRET, repr(row))
        self.assertNotIn(FIRST_SECRET, response.content.decode())
        self.assertEqual(response.json()['keyHint'], '••••1234')
        self.assertEqual(set(response.json()), {
            'id', 'name', 'provider', 'keyHint', 'isActive', 'version',
            'createdAt', 'updatedAt',
        })

    def test_settings_only_returns_owner_metadata_without_network(self) -> None:
        connection = self.connection()
        self.connection(self.bob, secret=SECOND_SECRET)
        with patch('requests.sessions.Session.request') as request:
            response = self.client.get('/api/personal-api/settings/')
        request.assert_not_called()
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(
            [row['id'] for row in payload['connections']], [connection['id']],
        )
        self.assertEqual(len(payload['preferences']), 4)
        self.assertFalse(payload['hasPaidSubscription'])
        self.assertTrue(payload['storageAvailable'])
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertNotIn(FIRST_SECRET, response.content.decode())
        self.assertNotIn(SECOND_SECRET, response.content.decode())

    @override_settings(PERSONAL_API_ENCRYPTION_KEY='')
    def test_missing_dedicated_key_disables_storage_without_secret_key_fallback(self):
        response = self.client.post('/api/personal-api/connections/', {
            'name': 'Images', 'provider': 'gemini', 'apiKey': FIRST_SECRET,
        }, format='json')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'PERSONAL_API_STORAGE_UNAVAILABLE')
        self.assertFalse(PersonalAPIConnection.objects.exists())
        self.assertFalse(self.client.get(
            '/api/personal-api/settings/',
        ).json()['storageAvailable'])

    def test_owner_cannot_read_change_or_revoke_other_users_connection(self) -> None:
        connection = self.connection(self.bob, secret=SECOND_SECRET)
        url = f"/api/personal-api/connections/{connection['id']}/"
        for response in (
            self.client.patch(url, {'apiKey': FIRST_SECRET}, format='json'),
            self.client.delete(url),
        ):
            self.assertEqual(response.status_code, 404)
        self.assertEqual(decrypt_secret(PersonalAPIConnection.objects.get(
            pk=connection['id'],
        )), SECOND_SECRET)

    def test_replacing_key_increments_version_but_renaming_does_not(self) -> None:
        connection = self.connection()
        url = f"/api/personal-api/connections/{connection['id']}/"
        renamed = self.client.patch(url, {'name': 'Renamed'}, format='json')
        self.assertEqual(renamed.json()['version'], 1)
        changed = self.client.patch(url, {'apiKey': SECOND_SECRET}, format='json')
        self.assertEqual(changed.json()['version'], 2)
        self.assertEqual(changed.json()['keyHint'], '••••5678')
        self.assertEqual(decrypt_secret(PersonalAPIConnection.objects.get(
            pk=connection['id'],
        )), SECOND_SECRET)

    def test_ciphertext_cannot_be_swapped_between_connections(self) -> None:
        first, second = self.connection(), self.connection()
        first_row = PersonalAPIConnection.objects.get(pk=first['id'])
        second_row = PersonalAPIConnection.objects.get(pk=second['id'])
        second_row.encrypted_key = first_row.encrypted_key
        with self.assertRaises(PersonalAPIError):
            decrypt_secret(second_row)

    def test_invalid_ciphertext_fails_closed_and_error_does_not_echo_it(self) -> None:
        connection = self.connection()
        row = PersonalAPIConnection.objects.get(pk=connection['id'])
        row.encrypted_key = FIRST_SECRET
        with self.assertRaises(PersonalAPIError) as caught:
            decrypt_secret(row)
        self.assertNotIn(FIRST_SECRET, str(caught.exception))

    def test_delete_scrubs_ciphertext_removes_routes_and_does_not_restore(self) -> None:
        connection = self.connection()
        self.configure(connection)
        response = self.client.delete(
            f"/api/personal-api/connections/{connection['id']}/",
        )
        self.assertEqual(response.status_code, 204)
        row = PersonalAPIConnection.objects.get(pk=connection['id'])
        self.assertEqual(row.encrypted_key, '')
        self.assertEqual(row.key_hint, '')
        self.assertFalse(row.is_active)
        self.assertEqual(row.version, 2)
        self.assertEqual(PersonalAPIPreference.objects.get(user=self.alice).routes, [])
        response = self.client.patch(
            f"/api/personal-api/connections/{connection['id']}/",
            {'isActive': True}, format='json',
        )
        self.assertEqual(response.status_code, 404)

    def test_account_closure_scrubs_credentials_and_preferences(self) -> None:
        connection = self.connection()
        self.configure(connection)
        close_user_account(self.alice, 'password')
        row = PersonalAPIConnection.objects.get(pk=connection['id'])
        self.assertEqual((row.encrypted_key, row.key_hint, row.name), ('', '', ''))
        self.assertFalse(row.is_active)
        self.assertFalse(PersonalAPIPreference.objects.filter(user=self.alice).exists())
        with self.assertRaises(PersonalAPIError):
            self.connection()

    def test_secret_and_unknown_fields_are_validated_without_echo(self) -> None:
        for change in (
            {'apiKey': 'with\nnewline'}, {'apiKey': 'short'},
            {'apiKey': 'x' * 4097}, {'baseUrl': 'http://127.0.0.1'},
        ):
            response = self.client.post('/api/personal-api/connections/', {
                'name': 'test', 'provider': 'gemini', 'apiKey': FIRST_SECRET, **change,
            }, format='json')
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(FIRST_SECRET, response.content.decode())

    def test_connection_count_is_bounded_but_multiple_same_provider_keys_allowed(self):
        for _ in range(20):
            self.connection()
        with self.assertRaises(PersonalAPIError) as caught:
            self.connection()
        self.assertEqual(caught.exception.code, 'PERSONAL_API_CONNECTION_LIMIT')


class PersonalAPIPreferenceTests(PersonalAPIBase):
    def test_ordered_routes_and_sources_are_saved_independently_per_modality(self):
        first, second = self.connection(), self.connection()
        image = self.configure(second, first)
        text = self.configure(first, modality='text', model='gemini/gemini-2.5-flash')
        self.assertEqual(image['routes'][0]['connectionId'], second['id'])
        self.assertEqual(text['routes'][0]['modelKey'], 'gemini/gemini-2.5-flash')
        self.assertEqual(generation_source(self.alice, 'image'), 'personal')
        self.assertEqual(generation_source(self.alice, 'music'), 'platform')

    def test_empty_personal_preference_is_valid_but_blocks_generation(self) -> None:
        response = self.client.put('/api/personal-api/preferences/text/', {
            'source': 'personal', 'routes': [],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {
            'modality': 'text', 'source': 'personal', 'routes': [],
        })
        with self.assertRaises(PersonalAPIError) as caught:
            resolve_generation_access(self.alice, 'text')
        self.assertEqual(caught.exception.code, 'PERSONAL_API_NOT_CONFIGURED')

    def test_routes_reject_other_owner_unknown_model_and_duplicate(self) -> None:
        connection, foreign = self.connection(), self.connection(self.bob)
        for routes in (
            [{'connectionId': foreign['id'], 'modelKey': 'gemini-flash-image'}],
            [{'connectionId': connection['id'], 'modelKey': 'unknown'}],
            [{'connectionId': connection['id'], 'modelKey': 'gemini-flash-image'}] * 2,
        ):
            response = self.client.put('/api/personal-api/preferences/image/', {
                'source': 'personal', 'routes': routes,
            }, format='json')
            self.assertIn(response.status_code, (400, 404))
        self.assertFalse(PersonalAPIPreference.objects.exists())

    def test_routes_reject_wrong_provider_and_unsupported_modality(self) -> None:
        connection = self.connection(provider='openrouter')
        with self.assertRaises(PersonalAPIError):
            self.configure(connection)
        response = self.client.put('/api/personal-api/preferences/video/', {
            'source': 'personal', 'routes': [],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_api_route_count_is_bounded(self) -> None:
        connection = self.connection()
        response = self.client.put('/api/personal-api/preferences/image/', {
            'source': 'personal', 'routes': [
                {'connectionId': connection['id'], 'modelKey': 'gemini-flash-image'},
            ] * 9,
        }, format='json')
        self.assertEqual(response.status_code, 400)


class PersonalAPIAccessTests(PersonalAPIBase):
    def test_paid_platform_always_requires_future_subscription_even_for_staff(self):
        self.alice.is_staff = self.alice.is_superuser = True
        self.alice.save()
        for modality in ('text', 'image', 'music', 'sound_effect'):
            with self.assertRaises(PersonalAPIError) as caught:
                resolve_generation_access(self.alice, modality, source='platform')
            self.assertEqual(caught.exception.code, 'CRAFT_SUBSCRIPTION_REQUIRED')

    def test_trusted_free_route_works_without_key_and_ignores_personal_default(self):
        self.configure(modality='text')
        snapshot = resolve_generation_access(self.alice, 'text', free_platform=True)
        self.assertEqual(snapshot['source'], 'platform')
        self.assertTrue(snapshot['freePlatform'])
        validate_generation_access(snapshot, user=self.alice)

    def test_image_key_does_not_enable_text_and_prompt_does_not_require_text(self):
        connection = self.connection()
        self.configure(connection)
        snapshot = resolve_generation_access(self.alice, 'image')
        self.assertEqual(snapshot['candidates'][0]['connectionId'], connection['id'])
        with self.assertRaises(PersonalAPIError):
            resolve_generation_access(self.alice, 'text', source='personal')

    def test_personal_snapshot_has_ordered_references_only_and_no_wallet_calls(self):
        first = self.connection()
        second = self.connection(secret=SECOND_SECRET)
        self.configure(second, first)
        with patch('w_craft_back.credits.services.reserve_generation') as reserve:
            snapshot = resolve_generation_access(self.alice, 'image')
            validate_generation_access(snapshot)
        reserve.assert_not_called()
        self.assertEqual([row['connectionId'] for row in snapshot['candidates']], [
            second['id'], first['id'],
        ])
        self.assertNotIn(FIRST_SECRET, json.dumps(snapshot))
        self.assertNotIn(SECOND_SECRET, json.dumps(snapshot))

    def test_explicit_model_outside_preferences_is_denied(self) -> None:
        connection = self.connection()
        self.configure(connection)
        with self.assertRaises(PersonalAPIError):
            resolve_generation_access(self.alice, 'image', model_key='gemini-imagen-4')

    def test_selected_primary_keeps_following_approved_models(self) -> None:
        connection = self.connection()
        save_preference(self.alice, 'image', source='personal', routes=[
            {'connectionId': connection['id'], 'modelKey': model_key}
            for model_key in ('gemini-flash-image', 'gemini-imagen-4')
        ])
        quote = resolve_generation_access(self.alice, 'image')
        enqueue = resolve_generation_access(
            self.alice, 'image', model_key=quote['candidates'][0]['modelKey'],
        )
        self.assertEqual(quote, enqueue)
        self.assertEqual([item['modelKey'] for item in enqueue['candidates']], [
            'gemini-flash-image', 'gemini-imagen-4',
        ])

    def test_selected_later_model_starts_chain_there_without_earlier_routes(self):
        connection = self.connection()
        save_preference(self.alice, 'image', source='personal', routes=[
            {'connectionId': connection['id'], 'modelKey': model_key}
            for model_key in (
                'gemini-flash-image', 'gemini-imagen-4', 'gemini-native',
            )
        ])
        snapshot = resolve_generation_access(
            self.alice, 'image', model_key='gemini-imagen-4',
        )
        self.assertEqual([item['modelKey'] for item in snapshot['candidates']], [
            'gemini-imagen-4', 'gemini-native',
        ])

    def test_incompatible_edit_and_reference_operations_are_rejected(self) -> None:
        connection = self.connection()
        self.configure(connection, model='gemini-imagen-4')
        resolve_generation_access(self.alice, 'image', operation='generate')
        for operation in ('edit', 'reference', 'structured'):
            with self.assertRaises(PersonalAPIError):
                resolve_generation_access(self.alice, 'image', operation=operation)

    def test_disabled_routes_are_skipped_without_changing_the_source(self) -> None:
        first, second = self.connection(), self.connection(secret=SECOND_SECRET)
        self.configure(first, second)
        update_connection(self.alice, first['id'], isActive=False)
        snapshot = resolve_generation_access(self.alice, 'image')
        self.assertEqual(snapshot['source'], 'personal')
        self.assertEqual(len(snapshot['candidates']), 1)
        self.assertEqual(snapshot['candidates'][0]['connectionId'], second['id'])

    def test_credential_resolution_is_user_and_snapshot_candidate_scoped(self):
        connection = self.connection()
        self.configure(connection)
        snapshot = resolve_generation_access(self.alice, 'image')
        candidate = snapshot['candidates'][0]
        self.assertEqual(credential_for_candidate(snapshot, candidate), FIRST_SECRET)
        with self.assertRaises(PersonalAPIError):
            credential_for_candidate(snapshot, candidate, user=self.bob)
        with self.assertRaises(PersonalAPIError):
            credential_for_candidate(
                snapshot, {**candidate, 'connectionId': str(uuid4())},
            )
        with self.assertRaises(PersonalAPIError):
            validate_generation_access(snapshot, user=self.bob)

    def test_replaced_revoked_and_disabled_credentials_invalidate_queued_jobs(self):
        for action in ('replace', 'revoke', 'disable'):
            connection = self.connection()
            self.configure(connection)
            snapshot = resolve_generation_access(self.alice, 'image')
            if action == 'replace':
                update_connection(self.alice, connection['id'], apiKey=SECOND_SECRET)
            elif action == 'revoke':
                revoke_connection(self.alice, connection['id'])
            else:
                update_connection(self.alice, connection['id'], isActive=False)
                update_connection(self.alice, connection['id'], isActive=True)
            with self.assertRaises(PersonalAPIError):
                validate_generation_access(snapshot)

    def test_preferences_change_does_not_change_queued_candidate(self) -> None:
        first, second = self.connection(), self.connection(secret=SECOND_SECRET)
        self.configure(first)
        snapshot = resolve_generation_access(self.alice, 'image')
        self.configure(second)
        self.assertEqual(credential_for_candidate(
            snapshot, snapshot['candidates'][0],
        ), FIRST_SECRET)

    def test_bad_first_candidate_allows_remaining_pinned_candidate(self) -> None:
        first, second = self.connection(), self.connection(secret=SECOND_SECRET)
        self.configure(first, second)
        snapshot = resolve_generation_access(self.alice, 'image')
        revoke_connection(self.alice, first['id'])
        validate_generation_access(snapshot)
        self.assertEqual(credential_for_candidate(
            snapshot, snapshot['candidates'][1],
        ), SECOND_SECRET)

    def test_account_closure_invalidates_personal_and_free_snapshots(self) -> None:
        connection = self.connection()
        self.configure(connection)
        snapshots = [resolve_generation_access(self.alice, 'image'),
                     resolve_generation_access(self.alice, 'text', free_platform=True)]
        close_user_account(self.alice, 'password')
        for snapshot in snapshots:
            with self.assertRaises(PersonalAPIError):
                validate_generation_access(snapshot)

    def test_auth_rejection_is_scoped_to_current_connection_version(self) -> None:
        first, second = self.connection(), self.connection(secret=SECOND_SECRET)
        self.configure(first, second)
        snapshot = resolve_generation_access(self.alice, 'image')
        reject_candidate(snapshot, snapshot['candidates'][0], reason='authentication')
        self.assertFalse(PersonalAPIConnection.objects.get(pk=first['id']).is_active)
        self.assertTrue(PersonalAPIConnection.objects.get(pk=second['id']).is_active)
        validate_generation_access(snapshot)
        update_connection(self.alice, second['id'], apiKey='replacement-key-0000')
        reject_candidate(snapshot, snapshot['candidates'][1], reason='authentication')
        self.assertTrue(PersonalAPIConnection.objects.get(pk=second['id']).is_active)

    def test_ambiguous_or_model_failures_do_not_disable_connection(self) -> None:
        connection = self.connection()
        self.configure(connection)
        snapshot = resolve_generation_access(self.alice, 'image')
        for reason in ('timeout', 'quota', 'model_unavailable'):
            reject_candidate(snapshot, snapshot['candidates'][0], reason=reason)
        validate_generation_access(snapshot)

    def test_malformed_snapshots_fail_closed_without_exposing_keys(self) -> None:
        connection = self.connection()
        self.configure(connection)
        original = resolve_generation_access(self.alice, 'image')
        cases = [
            None, {}, {**original, 'ownerId': True}, {**original, 'source': 'other'},
        ]
        for field, value in (
            ('connectionId', 'bad-uuid'), ('connectionVersion', True),
            ('provider', 'other'),
        ):
            snapshot = deepcopy(original)
            snapshot['candidates'][0][field] = value
            cases.append(snapshot)
        for snapshot in cases:
            with self.assertRaises(PersonalAPIError):
                validate_generation_access(snapshot)


class PersonalAPIApprovalTests(PersonalAPIBase):
    def test_preferences_change_cannot_change_the_approved_payer_or_chain(self):
        first, second = self.connection(), self.connection(secret=SECOND_SECRET)
        self.configure(first)
        snapshot = resolve_generation_access(self.alice, 'image')
        approval = sign_generation_access(snapshot)
        self.configure(second, source='platform')
        resolved = resolve_generation_access(
            self.alice, 'image', approval_token=approval,
        )
        self.assertEqual(resolved, snapshot)
        self.assertEqual(credential_for_candidate(
            resolved, resolved['candidates'][0],
        ), FIRST_SECRET)

    def test_changed_or_disabled_backup_requires_reapproval(self) -> None:
        for action in ('rotate', 'disable', 'revoke'):
            first, second = self.connection(), self.connection(secret=SECOND_SECRET)
            self.configure(first, second)
            approval = sign_generation_access(resolve_generation_access(
                self.alice, 'image',
            ))
            if action == 'rotate':
                update_connection(self.alice, second['id'], apiKey='new-key-value-9012')
            elif action == 'disable':
                update_connection(self.alice, second['id'], isActive=False)
            else:
                revoke_connection(self.alice, second['id'])
            with self.assertRaises(PersonalAPIError) as caught:
                resolve_generation_access(self.alice, 'image', approval_token=approval)
            self.assertEqual(caught.exception.code, 'GENERATION_APPROVAL_MISMATCH')

    def test_token_is_bound_to_owner_modality_source_and_primary_model(self):
        connection = self.connection()
        self.configure(connection)
        approval = sign_generation_access(resolve_generation_access(
            self.alice, 'image',
        ))
        for actor, modality, options in (
            (self.bob, 'image', {}),
            (self.alice, 'text', {}),
            (self.alice, 'image', {'source': 'platform'}),
            (self.alice, 'image', {'model_key': 'gemini-imagen-4'}),
        ):
            with self.assertRaises(PersonalAPIError) as caught:
                resolve_generation_access(
                    actor, modality, approval_token=approval, **options,
                )
            self.assertEqual(caught.exception.code, 'GENERATION_APPROVAL_MISMATCH')

    def test_tampered_and_expired_approvals_are_rejected(self) -> None:
        connection = self.connection()
        self.configure(connection)
        snapshot = resolve_generation_access(self.alice, 'image')
        approval = sign_generation_access(snapshot)
        with self.assertRaises(PersonalAPIError) as caught:
            resolve_generation_access(
                self.alice, 'image', approval_token=approval + 'x',
            )
        self.assertEqual(caught.exception.code, 'GENERATION_APPROVAL_INVALID')
        with patch('django.core.signing.time.time', return_value=1):
            expired = sign_generation_access(snapshot)
        with self.assertRaises(PersonalAPIError) as caught:
            resolve_generation_access(self.alice, 'image', approval_token=expired)
        self.assertEqual(caught.exception.code, 'GENERATION_APPROVAL_EXPIRED')

    def test_operation_can_tighten_without_adding_an_unapproved_route(self) -> None:
        connection = self.connection()
        save_preference(self.alice, 'image', source='personal', routes=[
            {'connectionId': connection['id'], 'modelKey': model_key}
            for model_key in ('gemini-flash-image', 'gemini-imagen-4')
        ])
        snapshot = resolve_generation_access(self.alice, 'image')
        approval = sign_generation_access(snapshot)
        resolved = resolve_generation_access(
            self.alice, 'image', operation='reference', approval_token=approval,
        )
        self.assertEqual(resolved['candidates'], snapshot['candidates'][:1])
        self.assertEqual(resolved['operation'], 'reference')

    def test_incompatible_primary_is_rejected_instead_of_changing_payer(self) -> None:
        connection = self.connection()
        save_preference(self.alice, 'image', source='personal', routes=[
            {'connectionId': connection['id'], 'modelKey': model_key}
            for model_key in ('gemini-imagen-4', 'gemini-flash-image')
        ])
        approval = sign_generation_access(resolve_generation_access(
            self.alice, 'image',
        ))
        with self.assertRaises(PersonalAPIError) as caught:
            resolve_generation_access(
                self.alice, 'image', operation='reference', approval_token=approval,
            )
        self.assertEqual(caught.exception.code, 'GENERATION_APPROVAL_MISMATCH')

    def test_signed_payload_omits_unexpected_secret_fields(self) -> None:
        connection = self.connection()
        self.configure(connection)
        snapshot = resolve_generation_access(self.alice, 'image')
        snapshot['apiKey'] = FIRST_SECRET
        snapshot['candidates'][0]['api_key'] = SECOND_SECRET
        approval = sign_generation_access(snapshot)
        payload = signing.loads(
            approval, salt='craft.personal-api.generation-access.v1',
        )
        self.assertNotIn(FIRST_SECRET, json.dumps(payload))
        self.assertNotIn(SECOND_SECRET, json.dumps(payload))
        self.assertEqual(set(payload), {
            'version', 'source', 'modality', 'operation', 'ownerId', 'candidates',
        })
        free = resolve_generation_access(self.alice, 'text', free_platform=True)
        self.assertEqual(sign_generation_access(free), '')
