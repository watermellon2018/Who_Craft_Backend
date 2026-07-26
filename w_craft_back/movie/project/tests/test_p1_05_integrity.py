"""Project aggregate ownership and cross-project integrity regressions."""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

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
from w_craft_back.characters.display_tree.models import MenuFolder
from w_craft_back.characters.pages.graph.model import GraphEdge, RelationshipType
from w_craft_back.movie.poster.models import (
    PosterFormat,
    PosterGenerationJob,
    PosterStyle,
    PosterVariant,
    ProjectPoster,
)
from w_craft_back.movie.project.dashboard_models import (
    AssetType,
    GenerationJobType as ProjectGenerationJobType,
    Location,
    MusicTrack,
    ProjectAsset,
    ProjectGenerationJob,
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
        user=owner_key,
        title=title,
        format="",
        annot="",
        desc="",
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
            user=self.actor_key,
            cur_project=self.project,
        )
        relation_type = RelationshipType.objects.create(
            name="Friend",
            translit="friend",
        )
        edge = GraphEdge.objects.create(
            user=self.actor_key,
            project=self.project,
            from_node="Hero",
            to_node="Friend",
            label=relation_type,
        )
        project_job = ProjectGenerationJob.objects.create(
            project=self.project,
            user=self.actor,
            job_type=ProjectGenerationJobType.SCENE_IMAGE,
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
            folder,
            edge,
            project_job,
            poster,
            poster_job,
            variant,
        ):
            entity.refresh_from_db()
            self.assertIsNone(entity.user_id)


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

    def test_graph_edge_direction_is_unique_per_project(self) -> None:
        _, other_actor_key = _create_user("graph-edge-other-actor")
        label_a = RelationshipType.objects.create(
            name="Friend",
            translit="graph-friend",
        )
        label_b = RelationshipType.objects.create(
            name="Enemy",
            translit="graph-enemy",
        )
        GraphEdge.objects.create(
            user=self.owner_key,
            project=self.project_a,
            from_node="A",
            to_node="B",
            label=label_a,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GraphEdge.objects.create(
                    user=other_actor_key,
                    project=self.project_a,
                    from_node="A",
                    to_node="B",
                    label=label_b,
                )

        other_project_edge = GraphEdge.objects.create(
            user=other_actor_key,
            project=self.project_b,
            from_node="A",
            to_node="B",
            label=label_b,
        )
        self.assertEqual(other_project_edge.project_id, self.project_b.id)

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
        GraphEdge = apps.get_model("w_craft_back", "GraphEdge")
        Project = apps.get_model("w_craft_back", "Project")
        RelationshipType = apps.get_model("w_craft_back", "RelationshipType")
        Scene = apps.get_model("w_craft_back", "Scene")
        UserKey = apps.get_model("w_craft_back", "UserKey")

        owner = User.objects.create(username="scene-migration-owner")
        actor = User.objects.create(username="graph-migration-actor")
        owner_key = UserKey.objects.create(
            user_id=owner.pk,
            key_digest="a" * 64,
            expires_at=timezone.now(),
        )
        actor_key = UserKey.objects.create(
            user_id=actor.pk,
            key_digest="b" * 64,
            expires_at=timezone.now(),
        )
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
        label = RelationshipType.objects.create(
            name="Friend",
            translit="migration-friend",
        )
        kept_edge = GraphEdge.objects.create(
            user_id=owner_key.pk,
            project_id=project_a.pk,
            from_node="A",
            to_node="B",
            label_id=label.pk,
        )
        removed_edge = GraphEdge.objects.create(
            user_id=actor_key.pk,
            project_id=project_a.pk,
            from_node="A",
            to_node="B",
            label_id=label.pk,
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
        self.kept_edge_id = kept_edge.pk
        self.removed_edge_id = removed_edge.pk
        self.graph_actor_key_id = actor_key.pk
        self.graph_label_id = label.pk

    def test_resequences_existing_scenes_and_enforces_constraints(self) -> None:
        GraphEdge = self.apps.get_model("w_craft_back", "GraphEdge")
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
        self.assertEqual(
            list(
                GraphEdge.objects.filter(
                    project_id=self.project_a_id,
                    from_node="A",
                    to_node="B",
                ).values_list("pk", flat=True)
            ),
            [self.kept_edge_id],
        )
        self.assertFalse(GraphEdge.objects.filter(pk=self.removed_edge_id).exists())

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GraphEdge.objects.create(
                    user_id=self.graph_actor_key_id,
                    project_id=self.project_a_id,
                    from_node="A",
                    to_node="B",
                    label_id=self.graph_label_id,
                )

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


class GraphEdgeConflictMigrationTests(TransactionTestCase):
    migrate_from = [("w_craft_back", "0042_userkey_authentication_lifecycle")]
    migrate_to = [("w_craft_back", "0043_project_aggregate_integrity")]

    def setUp(self) -> None:
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps
        self._seed_conflicting_edges()

    def tearDown(self) -> None:
        if hasattr(self, "conflicting_edge_id"):
            GraphEdge = self.old_apps.get_model("w_craft_back", "GraphEdge")
            GraphEdge.objects.filter(pk=self.conflicting_edge_id).delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _seed_conflicting_edges(self) -> None:
        User = self.old_apps.get_model("auth", "User")
        GraphEdge = self.old_apps.get_model("w_craft_back", "GraphEdge")
        Project = self.old_apps.get_model("w_craft_back", "Project")
        RelationshipType = self.old_apps.get_model(
            "w_craft_back",
            "RelationshipType",
        )
        UserKey = self.old_apps.get_model("w_craft_back", "UserKey")

        owner = User.objects.create(username="graph-conflict-owner")
        actor = User.objects.create(username="graph-conflict-actor")
        owner_key = UserKey.objects.create(
            user_id=owner.pk,
            key_digest="c" * 64,
            expires_at=timezone.now(),
        )
        actor_key = UserKey.objects.create(
            user_id=actor.pk,
            key_digest="d" * 64,
            expires_at=timezone.now(),
        )
        project = Project.objects.create(
            owner_id=owner.pk,
            title="Graph conflict",
            format="",
            annot="",
            desc="",
        )
        label_a = RelationshipType.objects.create(
            name="Friend",
            translit="conflict-friend",
        )
        label_b = RelationshipType.objects.create(
            name="Enemy",
            translit="conflict-enemy",
        )
        GraphEdge.objects.create(
            user_id=owner_key.pk,
            project_id=project.pk,
            from_node="A",
            to_node="B",
            label_id=label_a.pk,
        )
        conflicting_edge = GraphEdge.objects.create(
            user_id=actor_key.pk,
            project_id=project.pk,
            from_node="A",
            to_node="B",
            label_id=label_b.pk,
        )
        self.conflicting_edge_id = conflicting_edge.pk

    def test_conflicting_labels_require_manual_repair(self) -> None:
        executor = MigrationExecutor(connection)
        with self.assertRaisesRegex(
            RuntimeError,
            "Conflicting graph edge labels require manual repair",
        ):
            executor.migrate(self.migrate_to)
