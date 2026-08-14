from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


MIGRATE_FROM = [("w_craft_back", "0053_remove_project_legacy_fields")]
MIGRATE_TO = [("w_craft_back", "0054_normalize_runtime_values")]


class RuntimeValueNormalizationMigrationTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        self._seed_aliases(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_TO)
        self.apps = executor.loader.project_state(MIGRATE_TO).apps

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _seed_aliases(self, apps) -> None:
        User = apps.get_model("auth", "User")
        Project = apps.get_model("w_craft_back", "Project")
        StudioCharacter = apps.get_model("w_craft_back", "StudioCharacter")
        CharacterAppearance = apps.get_model(
            "w_craft_back",
            "CharacterAppearance",
        )
        MusicTrack = apps.get_model("w_craft_back", "MusicTrack")
        MusicAsset = apps.get_model("w_craft_back", "MusicAsset")
        ProjectAsset = apps.get_model("w_craft_back", "ProjectAsset")
        ProjectReference = apps.get_model("w_craft_back", "ProjectReference")
        ReferenceVersion = apps.get_model("w_craft_back", "ReferenceVersion")

        owner = User.objects.create(username="runtime-normalization-owner")
        projects = []
        for project_format in (
            "full-movie",
            "short-movie",
            "short-film",
            "marketing",
        ):
            projects.append(
                Project.objects.create(
                    owner_id=owner.pk,
                    title=f"Normalize {project_format}",
                    format=project_format,
                    annotation="",
                    synopsis="",
                )
            )
        self.project_ids = [project.pk for project in projects]

        character = StudioCharacter.objects.create(
            project_id=projects[0].pk,
            name="Alias hair",
        )
        for hair_length in ("buzz", "bob", "shoulder_length", "very_long"):
            CharacterAppearance.objects.create(
                character_id=character.pk,
                hair_length=hair_length,
            )
        self.character_id = character.pk

        track = MusicTrack.objects.create(
            project_id=projects[0].pk,
            title="Imported track",
            source="legacy",
        )
        self.track_id = track.pk
        asset = MusicAsset.objects.create(
            project_id=projects[0].pk,
            file="projects/music/imported.wav",
            asset_role="generated",
            origin="legacy",
            verification_status="legacy_unverified",
        )
        self.music_asset_id = asset.pk

        reference_asset = ProjectAsset.objects.create(
            project_id=projects[0].pk,
            file="projects/assets/imported-reference.png",
            asset_type="reference",
            title="Imported reference",
        )
        reference = ProjectReference.objects.create(
            project_id=projects[0].pk,
            title="Imported reference",
            category="other",
        )
        reference_version = ReferenceVersion.objects.create(
            reference_id=reference.pk,
            version_number=1,
            asset_id=reference_asset.pk,
            source_type="legacy",
        )
        self.reference_version_id = reference_version.pk

    def test_normalizes_all_known_runtime_aliases(self) -> None:
        Project = self.apps.get_model("w_craft_back", "Project")
        CharacterAppearance = self.apps.get_model(
            "w_craft_back",
            "CharacterAppearance",
        )
        MusicTrack = self.apps.get_model("w_craft_back", "MusicTrack")
        MusicAsset = self.apps.get_model("w_craft_back", "MusicAsset")
        ReferenceVersion = self.apps.get_model("w_craft_back", "ReferenceVersion")

        self.assertEqual(
            list(
                Project.objects.filter(pk__in=self.project_ids)
                .order_by("pk")
                .values_list("format", flat=True)
            ),
            ["feature_film", "short_film", "short_film", "commercial"],
        )
        self.assertCountEqual(
            CharacterAppearance.objects.filter(character_id=self.character_id)
            .values_list("hair_length", flat=True),
            ["short", "short", "medium", "long"],
        )
        self.assertEqual(
            MusicTrack.objects.get(pk=self.track_id).source,
            "manual",
        )
        music_asset = MusicAsset.objects.get(pk=self.music_asset_id)
        self.assertEqual(music_asset.origin, "upload")
        self.assertEqual(music_asset.verification_status, "pending")
        self.assertEqual(
            ReferenceVersion.objects.get(pk=self.reference_version_id).source_type,
            "upload",
        )

    def test_database_constraints_reject_noncanonical_values(self) -> None:
        Project = self.apps.get_model("w_craft_back", "Project")
        CharacterAppearance = self.apps.get_model(
            "w_craft_back",
            "CharacterAppearance",
        )
        MusicTrack = self.apps.get_model("w_craft_back", "MusicTrack")
        MusicAsset = self.apps.get_model("w_craft_back", "MusicAsset")
        ReferenceVersion = self.apps.get_model("w_craft_back", "ReferenceVersion")

        rejected_updates = (
            (
                CharacterAppearance.objects.filter(character_id=self.character_id),
                {"hair_length": "buzz"},
            ),
            (
                Project.objects.filter(pk=self.project_ids[0]),
                {"format": "full-movie"},
            ),
            (
                MusicTrack.objects.filter(pk=self.track_id),
                {"source": "legacy"},
            ),
            (
                MusicTrack.objects.filter(pk=self.track_id),
                {"audio_file": "projects/music/unversioned.wav"},
            ),
            (
                MusicAsset.objects.filter(pk=self.music_asset_id),
                {"origin": "legacy"},
            ),
            (
                MusicAsset.objects.filter(pk=self.music_asset_id),
                {"verification_status": "legacy_unverified"},
            ),
            (
                ReferenceVersion.objects.filter(pk=self.reference_version_id),
                {"source_type": "legacy"},
            ),
        )
        for queryset, update in rejected_updates:
            with self.subTest(update=update), self.assertRaises(IntegrityError):
                queryset.update(**update)


class UnsupportedProjectFormatMigrationTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        self.old_apps = executor.loader.project_state(MIGRATE_FROM).apps
        User = self.old_apps.get_model("auth", "User")
        Project = self.old_apps.get_model("w_craft_back", "Project")
        StudioCharacter = self.old_apps.get_model(
            "w_craft_back",
            "StudioCharacter",
        )
        CharacterAppearance = self.old_apps.get_model(
            "w_craft_back",
            "CharacterAppearance",
        )
        MusicTrack = self.old_apps.get_model("w_craft_back", "MusicTrack")
        MusicAsset = self.old_apps.get_model("w_craft_back", "MusicAsset")
        ProjectAsset = self.old_apps.get_model("w_craft_back", "ProjectAsset")
        ProjectReference = self.old_apps.get_model(
            "w_craft_back",
            "ProjectReference",
        )
        ReferenceVersion = self.old_apps.get_model(
            "w_craft_back",
            "ReferenceVersion",
        )
        owner = User.objects.create(username="unsupported-format-owner")
        self.project_id = Project.objects.create(
            owner_id=owner.pk,
            title="Unknown format",
            format="documentary",
            annotation="",
            synopsis="",
        ).pk
        self.alias_project_id = Project.objects.create(
            owner_id=owner.pk,
            title="Known alias",
            format="full-movie",
            annotation="",
            synopsis="",
        ).pk
        character = StudioCharacter.objects.create(
            project_id=self.alias_project_id,
            name="Unknown hair",
        )
        CharacterAppearance.objects.create(
            character_id=character.pk,
            hair_length="waist_length",
        )
        self.track_id = MusicTrack.objects.create(
            project_id=self.alias_project_id,
            title="Unknown source",
            source="cassette",
            audio_file="projects/music/unversioned.wav",
        ).pk
        self.music_asset_id = MusicAsset.objects.create(
            project_id=self.alias_project_id,
            file="projects/music/unknown.wav",
            asset_role="generated",
            origin="external",
            verification_status="quarantine",
        ).pk
        reference_asset = ProjectAsset.objects.create(
            project_id=self.alias_project_id,
            file="projects/assets/unknown.png",
            asset_type="reference",
            title="Unknown source",
        )
        reference = ProjectReference.objects.create(
            project_id=self.alias_project_id,
            title="Unknown source",
            category="other",
        )
        self.reference_version_id = ReferenceVersion.objects.create(
            reference_id=reference.pk,
            version_number=1,
            asset_id=reference_asset.pk,
            source_type="archive",
        ).pk

    def tearDown(self) -> None:
        Project = self.old_apps.get_model("w_craft_back", "Project")
        Project.objects.filter(
            pk__in=(self.project_id, self.alias_project_id)
        ).delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_unknown_format_aborts_without_guessing(self) -> None:
        executor = MigrationExecutor(connection)
        with self.assertRaises(RuntimeError) as context:
            executor.migrate(MIGRATE_TO)

        message = str(context.exception)
        for expected in (
            "Project.format",
            "documentary",
            "MusicTrack.source",
            "cassette",
            "MusicTrack.unversioned_audio_file",
            "MusicAsset.origin",
            "external",
            "MusicAsset.verification_status",
            "quarantine",
            "ReferenceVersion.source_type",
            "archive",
            "CharacterAppearance.hair_length",
            "waist_length",
        ):
            self.assertIn(expected, message)

        Project = self.old_apps.get_model("w_craft_back", "Project")
        self.assertEqual(
            Project.objects.get(pk=self.project_id).format,
            "documentary",
        )
        self.assertEqual(
            Project.objects.get(pk=self.alias_project_id).format,
            "full-movie",
        )
