"""Focused API coverage for the additive project roadmap contract."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetType,
    CharacterGenerationJob,
    CharacterStatus,
    GenerationJobStatus,
    GenerationJobType,
    StudioCharacter,
)
from w_craft_back.movie.music.models import (
    MusicAsset,
    MusicAssetVerificationStatus,
    MusicGenerationJob,
    MusicJobStatus,
    MusicTrackVersion,
)
from w_craft_back.movie.project.dashboard_models import (
    AssetType,
    MusicTrack,
    ProjectAsset,
    ProjectMember,
    ProjectMemberRole,
    Scene,
    SceneStoryboard,
    VideoShot,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.reference_library.models import (
    ProjectReference,
    ReferenceGenerationJob,
    ReferenceJobStatus,
    ReferenceOperation,
    ReferenceSourceType,
    ReferenceVersion,
)


class ProjectRoadmapApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user("roadmap-owner")
        self.user_key = UserKey.objects.create(user=self.owner)
        self.project = Project.objects.create(
            owner=self.owner,
            title="Roadmap",
            format="feature_film",
            annotation="",
            synopsis="",
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.owner,
            role=ProjectMemberRole.OWNER,
        )

    def _roadmap(self) -> dict:
        response = self.client.get(
            f"/api/projects/{self.project.id}/dashboard/",
            HTTP_X_USER_TOKEN=str(self.user_key.key),
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["roadmap"]

    def _steps(self) -> dict[str, dict]:
        return {step["key"]: step for step in self._roadmap()["steps"]}

    def _scene(self, order: int, *, content: str = "") -> Scene:
        return Scene.objects.create(
            project=self.project,
            title=f"Scene {order}",
            order=order,
            description=content,
        )

    def _asset(self, asset_type: str, name: str) -> ProjectAsset:
        return ProjectAsset.objects.create(
            project=self.project,
            uploaded_by=self.owner,
            file=f"projects/assets/{name}",
            asset_type=asset_type,
            title=name,
        )

    def _ready_storyboard(self, scene: Scene) -> SceneStoryboard:
        return SceneStoryboard.objects.create(
            scene=scene,
            asset=self._asset(AssetType.STORYBOARD, "storyboard.png"),
            source_scene_version=scene.version,
            created_by=self.owner,
            updated_by=self.owner,
        )

    def test_empty_project_has_stable_shape_and_required_next_action(self):
        roadmap = self._roadmap()
        self.assertEqual(roadmap["version"], 1)
        self.assertEqual(
            [step["key"] for step in roadmap["steps"]],
            [
                "script",
                "characters",
                "references",
                "music",
                "storyboard",
                "video",
            ],
        )
        self.assertEqual(
            [step["optional"] for step in roadmap["steps"]],
            [False, False, True, True, False, False],
        )
        self.assertTrue(
            all(step["availability"] == "available" for step in roadmap["steps"])
        )
        states = {step["key"]: step["state"] for step in roadmap["steps"]}
        self.assertEqual(
            states,
            {
                "script": "not_started",
                "characters": "not_started",
                "references": "not_started",
                "music": "not_started",
                "storyboard": "not_started",
                "video": "blocked",
            },
        )
        self.assertEqual(
            roadmap["nextAction"],
            {
                "stepKey": "script",
                "actionUrl": f"/project/{self.project.id}/script",
            },
        )

    def test_partial_then_ready_script_advances_to_storyboard(self):
        self._scene(1, content="Written")
        self._scene(2)

        partial = self._roadmap()
        partial_steps = {step["key"]: step for step in partial["steps"]}
        self.assertEqual(partial_steps["script"]["state"], "in_progress")
        self.assertEqual(partial_steps["script"]["progressPercent"], 50)
        self.assertEqual(
            partial_steps["script"]["metrics"],
            {"scenesTotal": 2, "scenesReady": 1},
        )
        self.assertEqual(partial["nextAction"]["stepKey"], "script")

        Scene.objects.filter(project=self.project, order=2).update(
            description="Also written"
        )
        ready = self._roadmap()
        ready_steps = {step["key"]: step for step in ready["steps"]}
        self.assertEqual(ready_steps["script"]["state"], "ready")
        self.assertEqual(ready_steps["characters"]["state"], "ready")
        self.assertEqual(ready_steps["storyboard"]["state"], "not_started")
        self.assertEqual(ready["nextAction"]["stepKey"], "storyboard")

    def test_visual_first_character_work_beats_untouched_script(self):
        StudioCharacter.objects.create(project=self.project, name="Mira")

        roadmap = self._roadmap()
        steps = {step["key"]: step for step in roadmap["steps"]}

        self.assertEqual(steps["script"]["state"], "not_started")
        self.assertEqual(steps["characters"]["state"], "in_progress")
        self.assertEqual(roadmap["nextAction"]["stepKey"], "characters")

    def test_ready_visual_first_character_advances_recommendation_to_script(self):
        character = StudioCharacter.objects.create(
            project=self.project,
            user=self.user_key,
            name="Mira",
            status=CharacterStatus.ACTIVE,
        )
        CharacterAsset.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            asset_type=CharacterAssetType.UPLOADED_REFERENCE,
            storage_path="tests/characters/mira.png",
            is_canonical=True,
        )

        roadmap = self._roadmap()
        steps = {step["key"]: step for step in roadmap["steps"]}

        self.assertEqual(steps["script"]["state"], "not_started")
        self.assertEqual(steps["characters"]["state"], "ready")
        self.assertEqual(steps["characters"]["progressPercent"], 100)
        self.assertEqual(roadmap["nextAction"]["stepKey"], "script")

    def test_missing_significant_character_requires_attention(self):
        StudioCharacter.objects.create(project=self.project, name="Mira")
        blocks = [
            {"id": "speaker", "type": "character", "text": "Mira"},
            *[
                {"id": f"line-{index}", "type": "dialogue", "text": "Line"}
                for index in range(6)
            ],
        ]
        Scene.objects.create(
            project=self.project,
            title="Dialogue",
            order=1,
            script_blocks=blocks,
        )

        roadmap = self._roadmap()
        characters = next(
            step for step in roadmap["steps"] if step["key"] == "characters"
        )

        self.assertEqual(characters["state"], "needs_attention")
        self.assertEqual(
            characters["metrics"],
            {"charactersTotal": 1, "charactersReady": 0},
        )
        self.assertEqual(characters["progressPercent"], 0)
        self.assertEqual(
            characters["blockers"],
            [{"code": "missingCharacters", "count": 1}],
        )
        self.assertEqual(roadmap["nextAction"]["stepKey"], "characters")

    def test_historical_failed_character_job_does_not_override_readiness(self):
        self._scene(1, content="A scene without significant characters")
        character = StudioCharacter.objects.create(
            project=self.project,
            user=self.user_key,
            name="Unused draft",
            status=CharacterStatus.ACTIVE,
        )
        CharacterAsset.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            asset_type=CharacterAssetType.UPLOADED_REFERENCE,
            storage_path="tests/characters/unused-draft.png",
            is_canonical=True,
        )
        CharacterGenerationJob.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            actor=self.user_key,
            job_type=GenerationJobType.INITIAL_VARIANTS,
            status=GenerationJobStatus.FAILED,
        )

        characters = self._steps()["characters"]

        self.assertEqual(characters["state"], "ready")
        self.assertEqual(
            characters["metrics"],
            {"charactersTotal": 1, "charactersReady": 1},
        )
        self.assertEqual(characters["blockers"], [])

    def test_character_progress_uses_missing_character_in_total(self):
        self._scene(1, content="Ready scene")
        for index in range(2):
            character = StudioCharacter.objects.create(
                project=self.project,
                user=self.user_key,
                name=f"Ready {index}",
                status=CharacterStatus.ACTIVE,
            )
            CharacterAsset.objects.create(
                character=character,
                project=self.project,
                user=self.user_key,
                asset_type=CharacterAssetType.UPLOADED_REFERENCE,
                storage_path=f"tests/characters/ready-{index}.png",
                is_canonical=True,
            )
        Scene.objects.filter(project=self.project).update(
            script_blocks=[
                {"id": "speaker", "type": "character", "text": "Missing"},
                *[
                    {"id": f"line-{index}", "type": "dialogue", "text": "Line"}
                    for index in range(6)
                ],
            ]
        )

        characters = self._steps()["characters"]

        self.assertEqual(characters["state"], "needs_attention")
        self.assertEqual(
            characters["metrics"],
            {"charactersTotal": 3, "charactersReady": 2},
        )
        self.assertEqual(characters["progressPercent"], 67)
        self.assertEqual(
            characters["blockers"],
            [{"code": "missingCharacters", "count": 1}],
        )

    def test_deleted_linked_character_remains_in_planned_total(self):
        character = StudioCharacter.objects.create(
            project=self.project,
            user=self.user_key,
            name="Mira",
            status=CharacterStatus.ACTIVE,
        )
        character_id = str(character.character_id)
        Scene.objects.create(
            project=self.project,
            title="Dialogue",
            order=1,
            script_blocks=[
                {
                    "id": "speaker",
                    "type": "character",
                    "text": "Mira",
                    "characterId": character_id,
                },
                *[
                    {
                        "id": f"line-{index}",
                        "type": "dialogue",
                        "text": "Line",
                        "characterId": character_id,
                    }
                    for index in range(6)
                ],
            ],
        )
        character.delete()

        characters = self._steps()["characters"]

        self.assertEqual(characters["state"], "needs_attention")
        self.assertEqual(
            characters["metrics"],
            {"charactersTotal": 1, "charactersReady": 0},
        )
        self.assertEqual(characters["progressPercent"], 0)
        self.assertEqual(
            characters["blockers"],
            [{"code": "missingCharacters", "count": 1}],
        )

    def test_optional_ready_stages_never_replace_required_next_action(self):
        self._scene(1, content="Ready script")
        reference = ProjectReference.objects.create(
            project=self.project,
            title="City",
            category="location",
            created_by=self.owner,
            updated_by=self.owner,
        )
        reference_version = ReferenceVersion.objects.create(
            reference=reference,
            version_number=1,
            asset=self._asset(AssetType.REFERENCE, "city.png"),
            source_type=ReferenceSourceType.GENERATED,
            created_by=self.owner,
        )
        reference.active_version = reference_version
        reference.save(update_fields=["active_version", "updated_at"])

        track = MusicTrack.objects.create(project=self.project, title="Theme")
        music_asset = MusicAsset.objects.create(
            project=self.project,
            file="tests/music/theme.wav",
            asset_role="generated",
            origin="upload",
            original_name="theme.wav",
            duration_seconds=Decimal("5.000"),
            verification_status=MusicAssetVerificationStatus.PENDING,
        )
        track_version = MusicTrackVersion.objects.create(
            track=track,
            version_number=1,
            asset=music_asset,
        )
        track.active_version = track_version
        track.save(update_fields=["active_version", "updated_at"])

        roadmap = self._roadmap()
        steps = {step["key"]: step for step in roadmap["steps"]}

        self.assertEqual(steps["references"]["state"], "ready")
        self.assertEqual(steps["references"]["progressPercent"], 100)
        self.assertEqual(steps["music"]["state"], "ready")
        self.assertEqual(steps["music"]["progressPercent"], 100)
        self.assertEqual(roadmap["nextAction"]["stepKey"], "storyboard")

    def test_optional_problem_states_never_replace_required_next_action(self):
        self._scene(1, content="Ready script")
        reference = ProjectReference.objects.create(
            project=self.project,
            title="Broken city",
            category="location",
            created_by=self.owner,
            updated_by=self.owner,
        )
        ReferenceGenerationJob.objects.create(
            project=self.project,
            reference=reference,
            actor=self.owner,
            operation=ReferenceOperation.GENERATE,
            status=ReferenceJobStatus.FAILED,
            idempotency_key="roadmap-reference-failed",
            request_fingerprint="reference-fingerprint",
        )
        MusicGenerationJob.objects.create(
            project=self.project,
            actor=self.owner,
            status=MusicJobStatus.QUEUED,
            idempotency_key="roadmap-music-active",
            request_fingerprint="music-fingerprint",
        )

        roadmap = self._roadmap()
        steps = {step["key"]: step for step in roadmap["steps"]}

        self.assertEqual(steps["references"]["state"], "needs_attention")
        self.assertEqual(steps["music"]["state"], "in_progress")
        self.assertEqual(roadmap["nextAction"]["stepKey"], "storyboard")

    def test_stale_storyboard_stays_in_progress_with_warning(self):
        scene = self._scene(1, content="Ready script")
        self._ready_storyboard(scene)
        Scene.objects.filter(pk=scene.pk).update(version=2)

        roadmap = self._roadmap()
        storyboard = next(
            step for step in roadmap["steps"] if step["key"] == "storyboard"
        )

        self.assertEqual(storyboard["state"], "in_progress")
        self.assertEqual(storyboard["metrics"]["scenesStale"], 1)
        self.assertIn(
            {"code": "staleStoryboards", "count": 1},
            storyboard["blockers"],
        )
        self.assertEqual(roadmap["nextAction"]["stepKey"], "storyboard")

    def test_stale_storyboard_does_not_jump_over_incomplete_script(self):
        scene = self._scene(1, content="Initially ready")
        self._ready_storyboard(scene)
        Scene.objects.filter(pk=scene.pk).update(description="", version=2)

        roadmap = self._roadmap()
        steps = {step["key"]: step for step in roadmap["steps"]}

        self.assertEqual(steps["script"]["state"], "in_progress")
        self.assertEqual(steps["storyboard"]["state"], "in_progress")
        self.assertNotIn(
            {"code": "scriptNotReady"},
            steps["storyboard"]["blockers"],
        )
        self.assertEqual(roadmap["nextAction"]["stepKey"], "script")

    def test_video_moves_from_not_started_to_partial_and_ready(self):
        scene = self._scene(1, content="Ready script")
        self._ready_storyboard(scene)

        initial = self._roadmap()
        initial_steps = {step["key"]: step for step in initial["steps"]}
        self.assertEqual(initial_steps["storyboard"]["state"], "ready")
        self.assertEqual(initial_steps["video"]["state"], "not_started")
        self.assertEqual(initial["nextAction"]["stepKey"], "video")

        shot = VideoShot.objects.create(
            project=self.project,
            scene=scene,
            order=1,
        )
        partial = self._roadmap()
        partial_video = next(
            step for step in partial["steps"] if step["key"] == "video"
        )
        self.assertEqual(partial_video["state"], "in_progress")
        self.assertEqual(
            partial_video["metrics"], {"shotsTotal": 1, "shotsReady": 0}
        )

        shot.final_asset = self._asset(AssetType.VIDEO, "final.mp4")
        shot.save(update_fields=["final_asset", "updated_at"])
        complete = self._roadmap()
        complete_video = next(
            step for step in complete["steps"] if step["key"] == "video"
        )
        self.assertEqual(complete_video["state"], "ready")
        self.assertEqual(complete_video["progressPercent"], 100)
        self.assertIsNone(complete["nextAction"])

    def test_started_video_needs_attention_when_upstream_becomes_invalid(self):
        scene = self._scene(1, content="Ready script")
        self._ready_storyboard(scene)
        VideoShot.objects.create(project=self.project, scene=scene, order=1)
        Scene.objects.filter(pk=scene.pk).update(version=2)

        video = self._steps()["video"]

        self.assertEqual(video["state"], "needs_attention")
        self.assertIn({"code": "storyboardNotReady"}, video["blockers"])

    def test_started_video_does_not_jump_over_unstarted_storyboard(self):
        scene = self._scene(1, content="Ready script")
        VideoShot.objects.create(project=self.project, scene=scene, order=1)

        roadmap = self._roadmap()
        steps = {step["key"]: step for step in roadmap["steps"]}

        self.assertEqual(steps["storyboard"]["state"], "not_started")
        self.assertEqual(steps["video"]["state"], "needs_attention")
        self.assertEqual(roadmap["nextAction"]["stepKey"], "storyboard")
