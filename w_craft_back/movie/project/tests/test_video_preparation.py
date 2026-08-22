"""API coverage for project video-preparation readiness."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    CharacterRole,
    CharacterStatus,
    StudioCharacter,
)
from w_craft_back.movie.project.dashboard_models import (
    AssetType,
    ProjectAsset,
    ProjectMember,
    ProjectMemberRole,
    Scene,
    SceneStoryboard,
)
from w_craft_back.movie.project.models import Project


class VideoPreparationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_key = self._make_user("video-preparation-owner")
        self.viewer, self.viewer_key = self._make_user("video-preparation-viewer")
        self.outsider, self.outsider_key = self._make_user(
            "video-preparation-outsider"
        )
        self.project = Project.objects.create(
            owner=self.owner,
            title="Preparation",
            format="feature_film",
            annotation="",
            synopsis="",
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.owner,
            role=ProjectMemberRole.OWNER,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.viewer,
            role=ProjectMemberRole.VIEWER,
        )

    @staticmethod
    def _make_user(username: str) -> tuple[User, UserKey]:
        user = User.objects.create_user(username=username, password="pw")
        return user, UserKey.objects.create(user=user)

    @property
    def preparation_url(self) -> str:
        return f"/api/projects/{self.project.id}/video/preparation/"

    @property
    def dashboard_url(self) -> str:
        return f"/api/projects/{self.project.id}/dashboard/"

    @staticmethod
    def _token(key: UserKey) -> dict:
        return {"HTTP_X_USER_TOKEN": str(key.key)}

    def _scene(
        self,
        title: str,
        order: int,
        *,
        script_blocks: list[dict] | None = None,
        version: int = 1,
    ) -> Scene:
        return Scene.objects.create(
            project=self.project,
            title=title,
            order=order,
            script_blocks=(
                script_blocks
                if script_blocks is not None
                else [{"id": f"action-{order}", "type": "action", "text": "Action"}]
            ),
            version=version,
            updated_by=self.owner,
        )

    def _storyboard(
        self,
        scene: Scene,
        *,
        source_version: int | None = None,
        confirmed_version: int | None = None,
    ) -> SceneStoryboard:
        asset = ProjectAsset.objects.create(
            project=self.project,
            file=f"projects/assets/storyboard-{scene.order}.png",
            asset_type=AssetType.STORYBOARD,
            title=f"Storyboard {scene.order}",
        )
        return SceneStoryboard.objects.create(
            scene=scene,
            asset=asset,
            source_scene_version=source_version or scene.version,
            confirmed_scene_version=confirmed_version,
            created_by=self.owner,
            updated_by=self.owner,
        )

    def _get(self, key: UserKey | None = None):
        headers = self._token(key or self.owner_key)
        return self.client.get(self.preparation_url, **headers)

    def test_access_and_zero_scene_storyboard_blocker(self):
        self.assertEqual(self.client.get(self.preparation_url).status_code, 401)
        self.assertEqual(self._get(self.outsider_key).status_code, 403)

        response = self._get(self.viewer_key)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["project"]["id"], self.project.id)
        self.assertEqual(payload["project"]["title"], self.project.title)
        self.assertFalse(payload["project"]["permissions"]["canEdit"])
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["taskCount"], 1)
        self.assertEqual(payload["missingCharacters"], [])
        self.assertEqual(payload["emptyScenes"], [])
        self.assertEqual(
            payload["storyboard"],
            {
                "ready": False,
                "progress": 0.0,
                "readyCount": 0,
                "totalCount": 0,
                "missingCount": 0,
                "staleCount": 0,
                "scenes": [],
            },
        )

    def test_draft_character_with_ready_visual_asset_remains_missing(self):
        character = StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Mira",
            role=CharacterRole.MAIN,
            status=CharacterStatus.DRAFT,
        )
        CharacterAsset.objects.create(
            character=character,
            project=self.project,
            asset_type=CharacterAssetType.PORTRAIT,
            status=CharacterAssetStatus.READY,
            image_url="/media/mira.png",
        )
        scene = self._scene(
            "Mira scene",
            1,
            script_blocks=[
                {"id": "mira", "type": "character", "text": "Mira"},
                {"id": "line", "type": "dialogue", "text": "Hello"},
            ],
        )
        self._storyboard(scene)

        payload = self._get().json()

        self.assertFalse(payload["ready"])
        self.assertEqual(payload["taskCount"], 1)
        self.assertEqual(
            payload["missingCharacters"],
            [{"name": "Mira", "dialogueCount": 1, "sceneCount": 1}],
        )
        self.assertEqual(payload["emptyScenes"], [])
        self.assertTrue(payload["storyboard"]["ready"])

    def test_empty_scene_uses_project_progress_content_semantics(self):
        template = "ИНТ. ЛОКАЦИЯ — ДЕНЬ"
        scene = self._scene(
            "Empty template",
            1,
            script_blocks=[
                {"id": "heading", "type": "scene_heading", "text": template}
            ],
        )
        scene.script_text = template
        scene.save(update_fields=["script_text"])
        self._storyboard(scene)

        payload = self._get().json()

        self.assertFalse(payload["ready"])
        self.assertEqual(payload["taskCount"], 1)
        self.assertEqual(
            payload["emptyScenes"],
            [{"sceneId": scene.id, "title": scene.title, "order": 1}],
        )
        self.assertTrue(payload["storyboard"]["ready"])

    def test_storyboard_reports_current_missing_and_stale_scenes(self):
        current = self._scene("Current", 1)
        missing = self._scene("Missing", 2)
        stale = self._scene("Stale", 3)
        self._storyboard(current)
        self._storyboard(stale)
        Scene.objects.filter(pk=stale.pk).update(version=2)

        payload = self._get().json()

        self.assertFalse(payload["ready"])
        self.assertEqual(payload["taskCount"], 1)
        self.assertEqual(payload["emptyScenes"], [])
        self.assertEqual(
            payload["storyboard"],
            {
                "ready": False,
                "progress": 1 / 3,
                "readyCount": 1,
                "totalCount": 3,
                "missingCount": 1,
                "staleCount": 1,
                "scenes": [
                    {
                        "sceneId": missing.id,
                        "title": "Missing",
                        "order": 2,
                        "status": "missing",
                        "currentVersion": 1,
                        "acceptedVersion": None,
                    },
                    {
                        "sceneId": stale.id,
                        "title": "Stale",
                        "order": 3,
                        "status": "stale",
                        "currentVersion": 2,
                        "acceptedVersion": 1,
                    },
                ],
            },
        )

    def test_task_count_sums_character_empty_scene_and_storyboard_groups(self):
        dialogue_blocks = [
            {"id": "speaker", "type": "character", "text": "Unknown"},
            *[
                {"id": f"line-{index}", "type": "dialogue", "text": "Line"}
                for index in range(6)
            ],
        ]
        self._scene("Dialogue", 1, script_blocks=dialogue_blocks)
        self._scene("Empty", 2, script_blocks=[])

        payload = self._get().json()

        self.assertFalse(payload["ready"])
        self.assertEqual(len(payload["missingCharacters"]), 1)
        self.assertEqual(len(payload["emptyScenes"]), 1)
        self.assertFalse(payload["storyboard"]["ready"])
        self.assertEqual(payload["taskCount"], 3)

    def test_ready_state_and_dashboard_compact_readiness_are_consistent(self):
        scene = self._scene("Ready", 1, version=2)
        self._storyboard(scene, source_version=1, confirmed_version=2)

        preparation = self._get().json()
        dashboard = self.client.get(
            self.dashboard_url,
            **self._token(self.owner_key),
        ).json()

        self.assertTrue(preparation["ready"])
        self.assertEqual(preparation["taskCount"], 0)
        self.assertEqual(preparation["storyboard"]["progress"], 1.0)
        self.assertEqual(
            dashboard["progress"]["readiness"]["videoPreparation"],
            {"ready": preparation["ready"], "taskCount": preparation["taskCount"]},
        )
