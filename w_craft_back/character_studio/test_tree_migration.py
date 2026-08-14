from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


MIGRATE_FROM = [("w_craft_back", "0051_image_model_flexibility")]
MIGRATE_TO = [("w_craft_back", "0052_remove_legacy_character_models")]


def _seed_project(apps, suffix: str):
    User = apps.get_model("auth", "User")
    UserKey = apps.get_model("w_craft_back", "UserKey")
    Project = apps.get_model("w_craft_back", "Project")
    user = User.objects.create(username=f"tree-migration-{suffix}")
    user_key = UserKey.objects.create(
        user_id=user.pk,
        key_digest=suffix.ljust(64, "0")[:64],
        expires_at=timezone.now(),
    )
    project = Project.objects.create(
        owner_id=user.pk,
        user_id=user_key.pk,
        title=f"Migration {suffix}",
        format="",
        annot="",
        desc="",
    )
    return user_key, project


class LegacyCharacterRemovalMigrationTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        self.old_apps = executor.loader.project_state(MIGRATE_FROM).apps

    def tearDown(self) -> None:
        self._clear_legacy_rows()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _clear_legacy_rows(self) -> None:
        for model_name in (
            "ItemFolder",
            "BiographyRelationships",
            "GoalsMotivation",
            "PersonalityTraits",
            "ProfessionHobbies",
            "TalentsAbilities",
            "GraphEdge",
            "RelationshipType",
            "Character",
        ):
            self.old_apps.get_model("w_craft_back", model_name).objects.all().delete()

    def test_guard_reports_every_populated_legacy_area(self) -> None:
        Character = self.old_apps.get_model("w_craft_back", "Character")
        GoalsMotivation = self.old_apps.get_model(
            "w_craft_back",
            "GoalsMotivation",
        )
        GraphEdge = self.old_apps.get_model("w_craft_back", "GraphEdge")
        ItemFolder = self.old_apps.get_model("w_craft_back", "ItemFolder")
        RelationshipType = self.old_apps.get_model(
            "w_craft_back",
            "RelationshipType",
        )
        user_key, project = _seed_project(self.old_apps, "guard")
        character = Character.objects.create(project_id=project.pk)
        GoalsMotivation.objects.create(character_id=character.pk)
        relation_type = RelationshipType.objects.create(
            name="Friend",
            translit="migration-guard-friend",
        )
        GraphEdge.objects.create(
            project_id=project.pk,
            label_id=relation_type.pk,
            from_node="A",
            to_node="B",
        )
        ItemFolder.objects.create(
            name="Legacy",
            is_folder=False,
            lft=1,
            rght=2,
            tree_id=1,
            level=0,
            user_id=user_key.pk,
            cur_project_id=project.pk,
            hero_id=character.pk,
        )

        executor = MigrationExecutor(connection)
        with self.assertRaises(RuntimeError) as context:
            executor.migrate(MIGRATE_TO)

        message = str(context.exception)
        for expected in (
            "Character",
            "GoalsMotivation",
            "GraphEdge",
            "RelationshipType",
            "ItemFolder.hero",
        ):
            self.assertIn(expected, message)

    def test_guard_rejects_unscoped_and_cross_project_placements(self) -> None:
        MenuFolder = self.old_apps.get_model("w_craft_back", "MenuFolder")
        ItemFolder = self.old_apps.get_model("w_craft_back", "ItemFolder")
        StudioCharacter = self.old_apps.get_model(
            "w_craft_back",
            "StudioCharacter",
        )
        user_key, first_project = _seed_project(self.old_apps, "tree-a")
        _, second_project = _seed_project(self.old_apps, "tree-b")
        character = StudioCharacter.objects.create(
            project_id=second_project.pk,
            user_id=user_key.pk,
            name="Wrong project",
            status="active",
        )
        ItemFolder.objects.create(
            name="Unscoped",
            is_folder=False,
            lft=1,
            rght=2,
            tree_id=1,
            level=0,
            user_id=user_key.pk,
            cur_project_id=None,
        )
        ItemFolder.objects.create(
            name="Cross project",
            is_folder=False,
            lft=1,
            rght=2,
            tree_id=2,
            level=0,
            user_id=user_key.pk,
            cur_project_id=first_project.pk,
            studio_character_id=character.pk,
        )
        foreign_parent = MenuFolder.objects.create(
            name="Foreign parent",
            is_folder=True,
            lft=1,
            rght=4,
            tree_id=3,
            level=0,
            user_id=user_key.pk,
            cur_project_id=second_project.pk,
        )
        ItemFolder.objects.create(
            name="Wrong parent",
            is_folder=False,
            lft=2,
            rght=3,
            tree_id=3,
            level=1,
            parent_id=foreign_parent.pk,
            user_id=user_key.pk,
            cur_project_id=first_project.pk,
        )

        executor = MigrationExecutor(connection)
        with self.assertRaises(RuntimeError) as context:
            executor.migrate(MIGRATE_TO)

        message = str(context.exception)
        self.assertIn("MenuFolder.cur_project", message)
        self.assertIn("MenuFolder.parent.project", message)
        self.assertIn("ItemFolder.studio_character.project", message)

    def test_guard_rejects_duplicate_character_placements(self) -> None:
        ItemFolder = self.old_apps.get_model("w_craft_back", "ItemFolder")
        StudioCharacter = self.old_apps.get_model(
            "w_craft_back",
            "StudioCharacter",
        )
        user_key, project = _seed_project(self.old_apps, "duplicates")
        character = StudioCharacter.objects.create(
            project_id=project.pk,
            user_id=user_key.pk,
            name="Repeated",
            status="active",
        )
        for tree_id in (1, 2):
            ItemFolder.objects.create(
                name=f"Repeated {tree_id}",
                is_folder=False,
                lft=1,
                rght=2,
                tree_id=tree_id,
                level=0,
                user_id=user_key.pk,
                cur_project_id=project.pk,
                studio_character_id=character.pk,
            )

        executor = MigrationExecutor(connection)
        with self.assertRaises(RuntimeError) as context:
            executor.migrate(MIGRATE_TO)

        self.assertIn(
            "ItemFolder.studio_character.duplicate",
            str(context.exception),
        )


class LegacyCharacterRemovalRoundTripTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        user_key, project = _seed_project(old_apps, "roundtrip")
        MenuFolder = old_apps.get_model("w_craft_back", "MenuFolder")
        ItemFolder = old_apps.get_model("w_craft_back", "ItemFolder")
        StudioCharacter = old_apps.get_model("w_craft_back", "StudioCharacter")
        character = StudioCharacter.objects.create(
            project_id=project.pk,
            user_id=user_key.pk,
            name="Mira",
            status="active",
        )
        folder = MenuFolder.objects.create(
            name="Cast",
            is_folder=True,
            lft=1,
            rght=4,
            tree_id=1,
            level=0,
            user_id=user_key.pk,
            cur_project_id=project.pk,
        )
        item = ItemFolder.objects.create(
            name="Mira",
            is_folder=False,
            lft=2,
            rght=3,
            tree_id=1,
            level=1,
            parent_id=folder.pk,
            user_id=user_key.pk,
            cur_project_id=project.pk,
            studio_character_id=character.pk,
        )
        self.folder_id = folder.pk
        self.item_id = item.pk

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_forward_and_reverse_preserve_tree_tables_and_placements(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        new_apps = executor.loader.project_state(MIGRATE_TO).apps
        MenuFolder = new_apps.get_model("w_craft_back", "MenuFolder")
        ItemFolder = new_apps.get_model("w_craft_back", "ItemFolder")

        self.assertTrue(MenuFolder.objects.filter(pk=self.folder_id).exists())
        self.assertTrue(ItemFolder.objects.filter(pk=self.item_id).exists())
        self.assertNotIn("hero", {field.name for field in ItemFolder._meta.fields})
        self.assertNotIn("user", {field.name for field in MenuFolder._meta.fields})
        for removed_model in (
            "Character",
            "GoalsMotivation",
            "GraphEdge",
            "RelationshipType",
        ):
            with self.assertRaises(LookupError):
                new_apps.get_model("w_craft_back", removed_model)
        tables = set(connection.introspection.table_names())
        self.assertIn(MenuFolder._meta.db_table, tables)
        self.assertIn(ItemFolder._meta.db_table, tables)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        restored_apps = executor.loader.project_state(MIGRATE_FROM).apps
        RestoredMenuFolder = restored_apps.get_model(
            "w_craft_back",
            "MenuFolder",
        )
        RestoredItemFolder = restored_apps.get_model(
            "w_craft_back",
            "ItemFolder",
        )
        self.assertTrue(
            RestoredMenuFolder.objects.filter(pk=self.folder_id).exists()
        )
        self.assertTrue(RestoredItemFolder.objects.filter(pk=self.item_id).exists())
        self.assertIn(
            "hero",
            {field.name for field in RestoredItemFolder._meta.fields},
        )
        self.assertIn(
            "user",
            {field.name for field in RestoredMenuFolder._meta.fields},
        )
        for restored_model in (
            "Character",
            "GoalsMotivation",
            "GraphEdge",
            "RelationshipType",
        ):
            model = restored_apps.get_model("w_craft_back", restored_model)
            self.assertFalse(model.objects.exists())
