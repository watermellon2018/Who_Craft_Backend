from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from w_craft_back.credits.models import CreditAccount, GenerationCharge
from w_craft_back.movie.project.dashboard_models import Scene, SceneStoryboard
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.storyboard.models import (
    StoryboardKeyframe,
    StoryboardKeyframeGeneration,
    StoryboardShot,
)


class StoryboardMigrationRollbackTests(TransactionTestCase):
    migrate_from = ("w_craft_back", "0062_project_progress_sources")
    migrate_to = ("w_craft_back", "0063_storyboard_domain")

    def test_reverse_removes_structured_storyboard_without_legacy_asset(self):
        owner = User.objects.create_user(username="storyboard-migration-owner")
        project = Project.objects.create(
            owner=owner,
            title="Migration storyboard",
            format="feature_film",
            annotation="",
            synopsis="",
        )
        scene = Scene.objects.create(
            project=project,
            title="Assetless scene",
            order=1,
            created_by=owner,
            updated_by=owner,
        )
        storyboard = SceneStoryboard.objects.create(
            scene=scene,
            asset=None,
            source_scene_version=scene.version,
            created_by=owner,
            updated_by=owner,
        )
        shot = StoryboardShot.objects.create(
            storyboard=storyboard,
            order=1,
            created_by=owner,
            updated_by=owner,
        )
        keyframe = StoryboardKeyframe.objects.create(
            shot=shot,
            type="start",
            position=0,
        )
        generation = StoryboardKeyframeGeneration.objects.create(
            keyframe=keyframe,
            actor=owner,
            request_snapshot={"migration": "rollback"},
            request_fingerprint="a" * 64,
            idempotency_key="migration-rollback-job",
        )
        account = CreditAccount.objects.create(
            user=owner,
            available_balance=9,
            reserved_balance=1,
        )
        charge = GenerationCharge.objects.create(
            account=account,
            project=project,
            domain="storyboard",
            job_id=str(generation.pk),
            provider="mock",
            model_name="reference-mock-v1",
            estimated_cost=1,
            reserved_amount=1,
        )

        try:
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_from])
            old_apps = executor.loader.project_state([self.migrate_from]).apps
            OldSceneStoryboard = old_apps.get_model(
                "w_craft_back",
                "SceneStoryboard",
            )
            self.assertFalse(
                OldSceneStoryboard.objects.filter(pk=storyboard.pk).exists()
            )
            OldGenerationCharge = old_apps.get_model(
                "w_craft_back",
                "GenerationCharge",
            )
            OldCreditAccount = old_apps.get_model(
                "w_craft_back",
                "CreditAccount",
            )
            rolled_back_charge = OldGenerationCharge.objects.get(pk=charge.pk)
            rolled_back_account = OldCreditAccount.objects.get(pk=account.pk)
            self.assertEqual(rolled_back_charge.status, "released")
            self.assertEqual(rolled_back_account.available_balance, 10)
            self.assertEqual(rolled_back_account.reserved_balance, 0)
        finally:
            MigrationExecutor(connection).migrate([self.migrate_to])
