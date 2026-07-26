"""Migration-level regression tests for the P0-05 ownership invariant."""

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import PROTECT, SET_NULL
from django.test import TransactionTestCase


class ProjectOwnershipMigrationTests(TransactionTestCase):
    migrate_from = [("w_craft_back", "0040_poster_job_error_http_status")]
    migrate_to = [("w_craft_back", "0041_enforce_single_project_owner")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        self._seed_legacy_state(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def _seed_legacy_state(self, apps):
        User = apps.get_model("auth", "User")
        UserKey = apps.get_model("w_craft_back", "UserKey")
        Project = apps.get_model("w_craft_back", "Project")
        ProjectMember = apps.get_model("w_craft_back", "ProjectMember")

        fallback_owner = User.objects.create(username="migration-fallback-owner")
        canonical_owner = User.objects.create(username="migration-canonical-owner")
        legacy_creator = User.objects.create(username="migration-legacy-creator")

        fallback_key = UserKey.objects.create(user_id=fallback_owner.pk)
        legacy_key = UserKey.objects.create(user_id=legacy_creator.pk)

        fallback_project = Project.objects.create(
            user_id=fallback_key.pk,
            owner_id=None,
            title="Fallback owner",
            format="series",
            annot="",
            desc="",
        )
        ProjectMember.objects.create(
            project_id=fallback_project.pk,
            user_id=legacy_creator.pk,
            role="owner",
        )

        canonical_project = Project.objects.create(
            user_id=legacy_key.pk,
            owner_id=canonical_owner.pk,
            title="Canonical owner wins",
            format="series",
            annot="",
            desc="",
        )
        ProjectMember.objects.create(
            project_id=canonical_project.pk,
            user_id=legacy_creator.pk,
            role="owner",
        )
        ProjectMember.objects.create(
            project_id=canonical_project.pk,
            user_id=canonical_owner.pk,
            role="editor",
        )

        self.fallback_project_id = fallback_project.pk
        self.fallback_owner_id = fallback_owner.pk
        self.canonical_project_id = canonical_project.pk
        self.canonical_owner_id = canonical_owner.pk
        self.legacy_creator_id = legacy_creator.pk

    def test_normalizes_data_and_applies_schema_guards(self):
        Project = self.apps.get_model("w_craft_back", "Project")
        ProjectMember = self.apps.get_model("w_craft_back", "ProjectMember")

        fallback_project = Project.objects.get(pk=self.fallback_project_id)
        self.assertEqual(fallback_project.owner_id, self.fallback_owner_id)
        self.assertEqual(
            list(
                ProjectMember.objects.filter(
                    project_id=fallback_project.pk,
                    role="owner",
                ).values_list("user_id", flat=True)
            ),
            [self.fallback_owner_id],
        )

        canonical_project = Project.objects.get(pk=self.canonical_project_id)
        self.assertEqual(canonical_project.owner_id, self.canonical_owner_id)
        self.assertEqual(
            ProjectMember.objects.get(
                project_id=canonical_project.pk,
                user_id=self.canonical_owner_id,
            ).role,
            "owner",
        )
        self.assertEqual(
            ProjectMember.objects.get(
                project_id=canonical_project.pk,
                user_id=self.legacy_creator_id,
            ).role,
            "admin",
        )

        owner_field = Project._meta.get_field("owner")
        creator_field = Project._meta.get_field("user")
        self.assertFalse(owner_field.null)
        self.assertIs(owner_field.remote_field.on_delete, PROTECT)
        self.assertTrue(creator_field.null)
        self.assertIs(creator_field.remote_field.on_delete, SET_NULL)

        other_user = self.apps.get_model("auth", "User").objects.create(
            username="migration-second-owner"
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProjectMember.objects.create(
                    project_id=canonical_project.pk,
                    user_id=other_user.pk,
                    role="owner",
                )
