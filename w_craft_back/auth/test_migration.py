"""Migration regression tests for digest-backed UserKey credentials."""

import hashlib
import uuid

from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from w_craft_back.auth.tokens import authenticate_access_token


class UserKeyDigestMigrationTests(TransactionTestCase):
    migrate_from = [("w_craft_back", "0041_enforce_single_project_owner")]
    migrate_to = [("w_craft_back", "0042_userkey_authentication_lifecycle")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        User = old_apps.get_model("auth", "User")
        UserKey = old_apps.get_model("w_craft_back", "UserKey")
        user = User.objects.create(username="legacy-token-owner")
        self.raw_token = str(uuid.uuid4())
        self.user_key_id = UserKey.objects.create(
            user_id=user.pk,
            key=self.raw_token,
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_plaintext_uuid_is_replaced_by_expiring_digest(self):
        UserKey = self.apps.get_model("w_craft_back", "UserKey")
        user_key = UserKey.objects.get(pk=self.user_key_id)

        self.assertEqual(
            user_key.key_digest,
            hashlib.sha256(self.raw_token.encode("utf-8")).hexdigest(),
        )
        self.assertGreater(user_key.expires_at, timezone.now())
        self.assertIsNone(user_key.refresh_digest)
        self.assertIsNone(user_key.refresh_expires_at)
        with self.assertRaises(FieldDoesNotExist):
            UserKey._meta.get_field("key")

        authenticated = authenticate_access_token(self.raw_token)
        self.assertEqual(authenticated.pk, self.user_key_id)
