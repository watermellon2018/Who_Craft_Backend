from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


MIGRATE_FROM = [("w_craft_back", "0052_remove_legacy_character_models")]
MIGRATE_TO = [("w_craft_back", "0053_remove_project_legacy_fields")]


class ProjectLegacyCleanupMigrationTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        self._seed_projects(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        self.apps = executor.loader.project_state(MIGRATE_TO).apps

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _seed_projects(self, apps) -> None:
        User = apps.get_model("auth", "User")
        UserKey = apps.get_model("w_craft_back", "UserKey")
        Genre = apps.get_model("w_craft_back", "Genre")
        Audience = apps.get_model("w_craft_back", "Audience")
        Project = apps.get_model("w_craft_back", "Project")

        owner = User.objects.create(username="project-cleanup-owner")
        user_key = UserKey.objects.create(
            user_id=owner.pk,
            key_digest="a" * 64,
            expires_at=timezone.now(),
        )
        genre = Genre.objects.create(name="Drama", translit="drama")
        audience = Audience.objects.create(name="Adults", translit="adults")

        legacy = Project.objects.create(
            owner_id=owner.pk,
            user_id=user_key.pk,
            title="Legacy cover",
            image="project/poster/legacy.png",
            cover_image="",
            format="feature_film",
            annot="Annotation",
            desc="Synopsis",
            description="Summary",
        )
        legacy.genre.add(genre)
        legacy.audience.add(audience)

        canonical = Project.objects.create(
            owner_id=owner.pk,
            user_id=user_key.pk,
            title="Canonical cover",
            image="",
            cover_image="projects/covers/canonical.png",
            format="feature_film",
            annot="",
            desc="",
            description="",
        )
        matching = Project.objects.create(
            owner_id=owner.pk,
            user_id=user_key.pk,
            title="Matching cover",
            image="project/poster/shared.png",
            cover_image="project/poster/shared.png",
            format="feature_film",
            annot="",
            desc="",
            description="",
        )
        self.legacy_id = legacy.pk
        self.canonical_id = canonical.pk
        self.matching_id = matching.pk
        self.genre_id = genre.pk
        self.audience_id = audience.pk

    def test_preserves_cover_and_project_field_values(self) -> None:
        Project = self.apps.get_model("w_craft_back", "Project")
        legacy = Project.objects.get(pk=self.legacy_id)
        canonical = Project.objects.get(pk=self.canonical_id)
        matching = Project.objects.get(pk=self.matching_id)

        self.assertEqual(legacy.cover_image.name, "project/poster/legacy.png")
        self.assertEqual(
            canonical.cover_image.name,
            "projects/covers/canonical.png",
        )
        self.assertEqual(matching.cover_image.name, "project/poster/shared.png")
        self.assertEqual(legacy.annotation, "Annotation")
        self.assertEqual(legacy.synopsis, "Synopsis")
        self.assertEqual(legacy.summary, "Summary")
        self.assertEqual(
            list(legacy.genres.values_list("id", flat=True)),
            [self.genre_id],
        )
        self.assertEqual(
            list(legacy.audiences.values_list("id", flat=True)),
            [self.audience_id],
        )
        with self.assertRaises(FieldDoesNotExist):
            Project._meta.get_field("user")
        with self.assertRaises(FieldDoesNotExist):
            Project._meta.get_field("image")

    def test_reverse_restores_equivalent_legacy_fields(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        Project = old_apps.get_model("w_craft_back", "Project")

        legacy = Project.objects.get(pk=self.legacy_id)
        canonical = Project.objects.get(pk=self.canonical_id)
        matching = Project.objects.get(pk=self.matching_id)
        self.assertEqual(legacy.image.name, "project/poster/legacy.png")
        self.assertEqual(canonical.image.name, "projects/covers/canonical.png")
        self.assertEqual(matching.image.name, "project/poster/shared.png")
        self.assertIsNone(legacy.user_id)
        self.assertIsNone(canonical.user_id)
        self.assertIsNone(matching.user_id)
        self.assertEqual(legacy.annot, "Annotation")
        self.assertEqual(legacy.desc, "Synopsis")
        self.assertEqual(legacy.description, "Summary")
        self.assertEqual(
            list(legacy.genre.values_list("id", flat=True)),
            [self.genre_id],
        )
        self.assertEqual(
            list(legacy.audience.values_list("id", flat=True)),
            [self.audience_id],
        )


class ProjectCoverConflictMigrationTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        self.old_apps = executor.loader.project_state(MIGRATE_FROM).apps

        User = self.old_apps.get_model("auth", "User")
        Project = self.old_apps.get_model("w_craft_back", "Project")
        owner = User.objects.create(username="project-cover-conflict")
        self.project_id = Project.objects.create(
            owner_id=owner.pk,
            title="Conflicting cover",
            image="project/poster/legacy.png",
            cover_image="projects/covers/canonical.png",
            format="feature_film",
            annot="",
            desc="",
            description="",
        ).pk

    def tearDown(self) -> None:
        Project = self.old_apps.get_model("w_craft_back", "Project")
        Project.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_conflicting_cover_names_abort_without_overwrite(self) -> None:
        executor = MigrationExecutor(connection)
        with self.assertRaisesRegex(RuntimeError, "different image and cover_image"):
            executor.migrate(MIGRATE_TO)

        Project = self.old_apps.get_model("w_craft_back", "Project")
        project = Project.objects.get(pk=self.project_id)
        self.assertEqual(project.image.name, "project/poster/legacy.png")
        self.assertEqual(project.cover_image.name, "projects/covers/canonical.png")
