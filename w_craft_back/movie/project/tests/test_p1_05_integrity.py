"""Project aggregate ownership and cross-project integrity regressions."""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterAppearance,
    CharacterAsset,
    CharacterAssetType,
    CharacterExpression,
    CharacterGenerationJob,
    CharacterImage,
    CharacterImageType,
    CharacterOutfit,
    CharacterRegion,
    CharacterRelationship,
    CharacterRevision,
    CharacterVariant,
    CharacterVersion,
    ExpressionType,
    GenerationJobType as CharacterGenerationJobType,
    RevisionChangeType,
    StudioCharacter,
)
from w_craft_back.character_studio.tree_models import MenuFolder
from w_craft_back.movie.poster.models import (
    PosterFormat,
    PosterGenerationJob,
    PosterStyle,
    PosterVariant,
    ProjectPoster,
)
from w_craft_back.movie.project.dashboard_models import (
    AssetType,
    Location,
    MusicTrack,
    ProjectAsset,
    Scene,
    SceneCharacter,
    SceneMusic,
)
from w_craft_back.movie.project.models import Project


def _create_user(username: str) -> tuple[User, UserKey]:
    user = User.objects.create_user(username=username, password="pw")
    return user, UserKey.objects.create(user=user)


def _create_project(owner: User, owner_key: UserKey, title: str) -> Project:
    return Project.objects.create(
        owner=owner,
        title=title,
        format="",
        annotation="",
        synopsis="",
    )


def _create_character(
    project: Project,
    actor: UserKey,
    name: str,
) -> StudioCharacter:
    return StudioCharacter.objects.create(
        project=project,
        user=actor,
        name=name,
    )


def _create_poster_job(
    project: Project,
    poster: ProjectPoster,
    actor: User,
    **kwargs,
) -> PosterGenerationJob:
    return PosterGenerationJob.objects.create(
        project=project,
        poster=poster,
        user=actor,
        prompt="poster",
        style=PosterStyle.CINEMATIC,
        format=PosterFormat.VERTICAL,
        aspect_ratio="2:3",
        **kwargs,
    )


class AttributionDeletionTests(TestCase):
    def setUp(self) -> None:
        self.owner, self.owner_key = _create_user("integrity-owner")
        self.actor, self.actor_key = _create_user("integrity-actor")
        self.project = _create_project(self.owner, self.owner_key, "Aggregate")

    def test_deleting_actor_preserves_project_aggregate(self) -> None:
        character = _create_character(self.project, self.actor_key, "Hero")
        asset = CharacterAsset.objects.create(
            character=character,
            project=self.project,
            user=self.actor_key,
            asset_type=CharacterAssetType.UPLOADED_REFERENCE,
        )
        character_job = CharacterGenerationJob.objects.create(
            character=character,
            project=self.project,
            user=self.actor_key,
            job_type=CharacterGenerationJobType.INITIAL_VARIANTS,
        )
        revision = CharacterRevision.objects.create(
            character=character,
            project=self.project,
            user=self.actor_key,
            revision_number=1,
            change_type=RevisionChangeType.INITIAL_CREATE,
        )
        folder = MenuFolder.objects.create(
            name="Characters",
            is_folder=True,
            cur_project=self.project,
        )
        poster = ProjectPoster.objects.create(
            project=self.project,
            user=self.actor,
        )
        poster_job = _create_poster_job(self.project, poster, self.actor)
        variant = PosterVariant.objects.create(
            project=self.project,
            poster=poster,
            job=poster_job,
            user=self.actor,
            image="projects/posters/variants/example.png",
        )

        self.actor.delete()

        for entity in (
            character,
            asset,
            character_job,
            revision,
            poster,
            poster_job,
            variant,
        ):
            entity.refresh_from_db()
            self.assertIsNone(entity.user_id)
        self.assertTrue(MenuFolder.objects.filter(pk=folder.pk).exists())


class CrossProjectValidationTests(TestCase):
    def setUp(self) -> None:
        self.owner, self.owner_key = _create_user("cross-project-owner")
        self.project_a = _create_project(self.owner, self.owner_key, "A")
        self.project_b = _create_project(self.owner, self.owner_key, "B")
        self.character_a = _create_character(
            self.project_a,
            self.owner_key,
            "A character",
        )
        self.character_b = _create_character(
            self.project_b,
            self.owner_key,
            "B character",
        )
        self.scene_a = Scene.objects.create(
            project=self.project_a,
            title="A scene",
            order=1,
        )

    def _create_character_b_links(self):
        appearance = CharacterAppearance.objects.create(character=self.character_b)
        asset = CharacterAsset.objects.create(
            character=self.character_b,
            project=self.project_b,
            user=self.owner_key,
            asset_type=CharacterAssetType.UPLOADED_REFERENCE,
        )
        outfit = CharacterOutfit.objects.create(
            character=self.character_b,
            name="B outfit",
            reference_image=asset,
        )
        version = CharacterVersion.objects.create(
            character=self.character_b,
            version_name="B version",
            appearance=appearance,
            outfit=outfit,
            reference_image=asset,
        )
        job = CharacterGenerationJob.objects.create(
            character=self.character_b,
            project=self.project_b,
            user=self.owner_key,
            job_type=CharacterGenerationJobType.INITIAL_VARIANTS,
        )
        variant = CharacterVariant.objects.create(
            job=job,
            character=self.character_b,
            asset=asset,
            variant_index=0,
            region=CharacterRegion.FULL_CHARACTER,
        )
        revision = CharacterRevision.objects.create(
            character=self.character_b,
            project=self.project_b,
            user=self.owner_key,
            revision_number=1,
            source_variant=variant,
            source_job=job,
            reference_image=asset,
            appearance=appearance,
            outfit=outfit,
            version=version,
            change_type=RevisionChangeType.INITIAL_CREATE,
        )
        return {
            "appearance": appearance,
            "asset": asset,
            "outfit": outfit,
            "version": version,
            "job": job,
            "variant": variant,
            "revision": revision,
        }

    def test_character_rejects_foreign_active_links(self) -> None:
        links = self._create_character_b_links()
        invalid_links = {
            "active_appearance": links["appearance"],
            "active_outfit": links["outfit"],
            "active_version": links["version"],
            "current_revision": links["revision"],
            "canonical_reference_image": links["asset"],
        }

        for field_name, related in invalid_links.items():
            with self.subTest(field=field_name):
                setattr(self.character_a, field_name, related)
                with self.assertRaises(ValidationError):
                    self.character_a.save()
                setattr(self.character_a, field_name, None)

    def test_character_link_models_reject_foreign_character_objects(self) -> None:
        links = self._create_character_b_links()
        invalid_creates = (
            lambda: CharacterImage.objects.create(
                character=self.character_a,
                asset=links["asset"],
                image_type=CharacterImageType.PORTRAIT,
            ),
            lambda: CharacterOutfit.objects.create(
                character=self.character_a,
                name="Invalid outfit",
                reference_image=links["asset"],
            ),
            lambda: CharacterVersion.objects.create(
                character=self.character_a,
                version_name="Invalid appearance",
                appearance=links["appearance"],
            ),
            lambda: CharacterVersion.objects.create(
                character=self.character_a,
                version_name="Invalid outfit",
                outfit=links["outfit"],
            ),
            lambda: CharacterVersion.objects.create(
                character=self.character_a,
                version_name="Invalid reference",
                reference_image=links["asset"],
            ),
            lambda: CharacterExpression.objects.create(
                character=self.character_a,
                expression_type=ExpressionType.NEUTRAL,
                asset=links["asset"],
            ),
        )

        for invalid_create in invalid_creates:
            with self.subTest(create=invalid_create), self.assertRaises(
                ValidationError
            ):
                invalid_create()

    def test_variant_and_revision_reject_foreign_character_links(self) -> None:
        links = self._create_character_b_links()
        job_a = CharacterGenerationJob.objects.create(
            character=self.character_a,
            project=self.project_a,
            user=self.owner_key,
            job_type=CharacterGenerationJobType.INITIAL_VARIANTS,
        )

        invalid_variants = (
            lambda: CharacterVariant.objects.create(
                job=links["job"],
                character=self.character_a,
                variant_index=0,
                region=CharacterRegion.FULL_CHARACTER,
            ),
            lambda: CharacterVariant.objects.create(
                job=job_a,
                character=self.character_a,
                asset=links["asset"],
                variant_index=0,
                region=CharacterRegion.FULL_CHARACTER,
            ),
        )
        for invalid_create in invalid_variants:
            with self.subTest(create=invalid_create), self.assertRaises(
                ValidationError
            ):
                invalid_create()

        revision_links = {
            "source_variant": links["variant"],
            "source_job": links["job"],
            "reference_image": links["asset"],
            "appearance": links["appearance"],
            "outfit": links["outfit"],
            "version": links["version"],
        }
        for field_name, related in revision_links.items():
            with self.subTest(field=field_name), self.assertRaises(ValidationError):
                CharacterRevision.objects.create(
                    character=self.character_a,
                    project=self.project_a,
                    user=self.owner_key,
                    revision_number=1,
                    change_type=RevisionChangeType.MANUAL_UPDATE,
                    **{field_name: related},
                )

        variant_a = CharacterVariant.objects.create(
            job=job_a,
            character=self.character_a,
            variant_index=0,
            region=CharacterRegion.FULL_CHARACTER,
        )
        other_job_a = CharacterGenerationJob.objects.create(
            character=self.character_a,
            project=self.project_a,
            user=self.owner_key,
            job_type=CharacterGenerationJobType.INITIAL_VARIANTS,
        )
        with self.assertRaises(ValidationError):
            CharacterRevision.objects.create(
                character=self.character_a,
                project=self.project_a,
                user=self.owner_key,
                revision_number=1,
                source_variant=variant_a,
                source_job=other_job_a,
                change_type=RevisionChangeType.MANUAL_UPDATE,
            )

    def test_poster_rejects_selected_variant_from_another_poster(self) -> None:
        poster_a = ProjectPoster.objects.create(project=self.project_a, user=self.owner)
        poster_b = ProjectPoster.objects.create(project=self.project_b, user=self.owner)
        job_b = _create_poster_job(self.project_b, poster_b, self.owner)
        variant_b = PosterVariant.objects.create(
            project=self.project_b,
            poster=poster_b,
            job=job_b,
            user=self.owner,
            image="projects/posters/variants/b.png",
        )

        poster_a.selected_variant = variant_b
        with self.assertRaises(ValidationError):
            poster_a.save()

    def test_poster_job_rejects_foreign_reference_links(self) -> None:
        poster_a = ProjectPoster.objects.create(project=self.project_a, user=self.owner)
        poster_b = ProjectPoster.objects.create(project=self.project_b, user=self.owner)
        asset_b = ProjectAsset.objects.create(
            project=self.project_b,
            uploaded_by=self.owner,
            file="projects/assets/reference.png",
            asset_type=AssetType.REFERENCE,
        )
        job_b = _create_poster_job(self.project_b, poster_b, self.owner)
        variant_b = PosterVariant.objects.create(
            project=self.project_b,
            poster=poster_b,
            job=job_b,
            user=self.owner,
            image="projects/posters/variants/reference.png",
        )

        invalid_jobs = (
            lambda: _create_poster_job(
                self.project_a,
                poster_a,
                self.owner,
                reference_asset=asset_b,
            ),
            lambda: _create_poster_job(
                self.project_a,
                poster_a,
                self.owner,
                source_variant=variant_b,
            ),
        )
        for invalid_create in invalid_jobs:
            with self.subTest(create=invalid_create), self.assertRaises(
                ValidationError
            ):
                invalid_create()

    def test_scene_rejects_location_from_another_project(self) -> None:
        location_b = Location.objects.create(project=self.project_b, name="B")
        with self.assertRaises(ValidationError):
            Scene.objects.create(
                project=self.project_a,
                title="Invalid",
                order=2,
                location=location_b,
            )

    def test_scene_character_rejects_another_project(self) -> None:
        with self.assertRaises(ValidationError):
            SceneCharacter.objects.create(
                scene=self.scene_a,
                character=self.character_b,
            )

    def test_scene_music_rejects_another_project(self) -> None:
        track_b = MusicTrack.objects.create(project=self.project_b, title="B")
        with self.assertRaises(ValidationError):
            SceneMusic.objects.create(scene=self.scene_a, track=track_b)

    def test_character_entities_reject_another_project(self) -> None:
        invalid_creates = (
            lambda: CharacterAsset.objects.create(
                character=self.character_a,
                project=self.project_b,
                user=self.owner_key,
                asset_type=CharacterAssetType.UPLOADED_REFERENCE,
            ),
            lambda: CharacterGenerationJob.objects.create(
                character=self.character_a,
                project=self.project_b,
                user=self.owner_key,
                job_type=CharacterGenerationJobType.INITIAL_VARIANTS,
            ),
            lambda: CharacterRevision.objects.create(
                character=self.character_a,
                project=self.project_b,
                user=self.owner_key,
                revision_number=1,
                change_type=RevisionChangeType.INITIAL_CREATE,
            ),
        )
        for invalid_create in invalid_creates:
            with self.subTest(create=invalid_create), self.assertRaises(
                ValidationError
            ):
                invalid_create()

    def test_relationship_rejects_cross_project_character(self) -> None:
        with self.assertRaises(ValidationError):
            CharacterRelationship.objects.create(
                project=self.project_a,
                source_character=self.character_a,
                target_character=self.character_b,
                relation_type="friend",
            )

    def test_poster_job_rejects_another_project(self) -> None:
        poster_a = ProjectPoster.objects.create(
            project=self.project_a,
            user=self.owner,
        )
        with self.assertRaises(ValidationError):
            _create_poster_job(self.project_b, poster_a, self.owner)

    def test_poster_variant_rejects_mixed_job_poster_project(self) -> None:
        poster_a = ProjectPoster.objects.create(
            project=self.project_a,
            user=self.owner,
        )
        poster_b = ProjectPoster.objects.create(
            project=self.project_b,
            user=self.owner,
        )
        job_a = _create_poster_job(self.project_a, poster_a, self.owner)
        with self.assertRaises(ValidationError):
            PosterVariant.objects.create(
                project=self.project_b,
                poster=poster_b,
                job=job_a,
                user=self.owner,
                image="projects/posters/variants/invalid.png",
            )


class SceneOrderingTests(TestCase):
    def setUp(self) -> None:
        owner, owner_key = _create_user("scene-order-owner")
        self.project = _create_project(owner, owner_key, "Ordered")
        self.other_project = _create_project(owner, owner_key, "Other")

    def test_scene_order_must_be_positive(self) -> None:
        with self.assertRaises(ValidationError):
            Scene.objects.create(project=self.project, title="Zero", order=0)

    def test_scene_order_is_unique_per_project(self) -> None:
        Scene.objects.create(project=self.project, title="First", order=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Scene.objects.create(project=self.project, title="Duplicate", order=1)

        other = Scene.objects.create(
            project=self.other_project,
            title="Other first",
            order=1,
        )
        self.assertEqual(other.order, 1)


class SceneOrderingMigrationTests(TransactionTestCase):
    migrate_from = [("w_craft_back", "0042_userkey_authentication_lifecycle")]
    migrate_to = [("w_craft_back", "0043_project_aggregate_integrity")]

    def setUp(self) -> None:
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        self._seed_scenes(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _seed_scenes(self, apps) -> None:
        User = apps.get_model("auth", "User")
        Project = apps.get_model("w_craft_back", "Project")
        Scene = apps.get_model("w_craft_back", "Scene")

        owner = User.objects.create(username="scene-migration-owner")
        project_a = Project.objects.create(
            owner_id=owner.pk,
            title="A",
            format="",
            annot="",
            desc="",
        )
        project_b = Project.objects.create(
            owner_id=owner.pk,
            title="B",
            format="",
            annot="",
            desc="",
        )
        scenes = (
            Scene.objects.create(project_id=project_a.pk, title="Zero", order=0),
            Scene.objects.create(project_id=project_a.pk, title="Two A", order=2),
            Scene.objects.create(project_id=project_a.pk, title="Two B", order=2),
            Scene.objects.create(project_id=project_b.pk, title="Nine", order=9),
        )
        self.project_a_id = project_a.pk
        self.project_b_id = project_b.pk
        self.scene_ids = [scene.pk for scene in scenes]

    def test_resequences_existing_scenes_and_enforces_constraints(self) -> None:
        Scene = self.apps.get_model("w_craft_back", "Scene")

        self.assertEqual(
            list(
                Scene.objects.filter(project_id=self.project_a_id)
                .order_by("order")
                .values_list("order", flat=True)
            ),
            [1, 2, 3],
        )
        self.assertEqual(Scene.objects.get(project_id=self.project_b_id).order, 1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Scene.objects.create(
                    project_id=self.project_a_id,
                    title="Invalid zero",
                    order=0,
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Scene.objects.create(
                    project_id=self.project_a_id,
                    title="Duplicate",
                    order=1,
                )
