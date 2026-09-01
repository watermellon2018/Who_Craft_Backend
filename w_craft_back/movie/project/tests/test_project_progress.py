from fractions import Fraction

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    StudioCharacter,
)
from w_craft_back.movie.project.dashboard_models import (
    AssetType,
    ProjectAsset,
    ProjectMember,
    ProjectMemberRole,
    Scene,
    SceneCharacter,
    SceneStoryboard,
    VideoShot,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.progress_service import (
    ProjectProgressSnapshot,
    calculate_project_progress,
)


def _project(owner: User, title="Progress") -> Project:
    project = Project.objects.create(
        owner=owner,
        title=title,
        format="feature_film",
        annotation="",
        synopsis="",
    )
    ProjectMember.objects.create(
        project=project,
        user=owner,
        role=ProjectMemberRole.OWNER,
    )
    return project


def _asset(project: Project, asset_type: str, name: str) -> ProjectAsset:
    return ProjectAsset.objects.create(
        project=project,
        file=f"projects/assets/{name}",
        asset_type=asset_type,
        title=name,
    )


class ProjectProgressServiceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("progress-owner")
        self.project = _project(self.owner)

    def test_calculates_all_components_from_current_domain_state(self):
        anna = StudioCharacter.objects.create(project=self.project, name="Анна")
        maxim = StudioCharacter.objects.create(project=self.project, name="Максим")
        victor = StudioCharacter.objects.create(project=self.project, name="Виктор")
        extra = StudioCharacter.objects.create(project=self.project, name="Эпизод")

        dialogue = [
            {
                "id": f"dialogue-{index}",
                "type": "dialogue",
                "text": f"Реплика {index}",
                "characterId": str(victor.character_id),
            }
            for index in range(6)
        ]
        scenes = [
            Scene.objects.create(
                project=self.project,
                title="Сцена 1",
                order=1,
                description="Описание",
                script_blocks=dialogue,
            ),
            Scene.objects.create(
                project=self.project,
                title="Сцена 2",
                order=2,
                script_blocks=[
                    {"id": "action", "type": "action", "text": "Действие"}
                ],
            ),
            Scene.objects.create(
                project=self.project,
                title="Сцена 3",
                order=3,
                script_text="Диалог в старом формате",
                script_blocks=[
                    {
                        "id": f"extra-dialogue-{index}",
                        "type": "dialogue",
                        "text": f"Эпизодическая реплика {index}",
                        "characterId": str(extra.character_id),
                    }
                    for index in range(5)
                ],
            ),
            Scene.objects.create(
                project=self.project,
                title="Сцена 4",
                order=4,
            ),
        ]
        for scene in scenes[:3]:
            SceneCharacter.objects.create(scene=scene, character=anna)
        for scene in scenes[:2]:
            SceneCharacter.objects.create(scene=scene, character=maxim)
        SceneCharacter.objects.create(scene=scenes[0], character=victor)
        SceneCharacter.objects.create(scene=scenes[2], character=extra)

        CharacterAsset.objects.create(
            character=anna,
            project=self.project,
            asset_type=CharacterAssetType.PORTRAIT,
            status=CharacterAssetStatus.READY,
            image_url="/media/anna.png",
        )

        first_storyboard = _asset(
            self.project,
            AssetType.STORYBOARD,
            "storyboard-1.png",
        )
        SceneStoryboard.objects.create(
            scene=scenes[0],
            asset=first_storyboard,
            source_scene_version=scenes[0].version,
        )
        stale_storyboard = _asset(
            self.project,
            AssetType.STORYBOARD,
            "storyboard-2.png",
        )
        SceneStoryboard.objects.create(
            scene=scenes[1],
            asset=stale_storyboard,
            source_scene_version=scenes[1].version,
        )
        Scene.objects.filter(pk=scenes[1].pk).update(version=2)

        final_video = _asset(self.project, AssetType.VIDEO, "final-1.mp4")
        _asset(self.project, AssetType.VIDEO, "unused-attempt.mp4")
        VideoShot.objects.create(
            project=self.project,
            scene=scenes[0],
            order=1,
            final_asset=final_video,
        )
        VideoShot.objects.create(
            project=self.project,
            scene=scenes[1],
            order=1,
        )

        snapshot = calculate_project_progress(self.project)

        self.assertEqual(snapshot.script, Fraction(3, 4))
        self.assertEqual(snapshot.characters, Fraction(1, 2))
        self.assertEqual(snapshot.storyboard, Fraction(1, 4))
        self.assertEqual(snapshot.video, Fraction(1, 2))
        self.assertEqual(snapshot.overall, Fraction(39, 80))
        self.assertEqual(
            [scene.scene_id for scene in snapshot.storyboard_review_scenes],
            [scenes[1].id],
        )
        self.assertEqual(snapshot.as_percentage_payload()["overall"], 49)

    def test_empty_project_and_character_na_are_normalized(self):
        snapshot = calculate_project_progress(self.project)
        self.assertEqual(snapshot.script, 0)
        self.assertIsNone(snapshot.characters)
        self.assertEqual(snapshot.storyboard, 0)
        self.assertEqual(snapshot.video, 0)
        self.assertEqual(snapshot.overall, 0)

        normalized = ProjectProgressSnapshot(
            script=Fraction(1, 1),
            characters=None,
            storyboard=Fraction(0, 1),
            video=Fraction(0, 1),
            storyboard_review_scenes=(),
        )
        self.assertEqual(normalized.overall, Fraction(1, 4))

    def test_scene_notes_are_content_but_whitespace_is_empty(self):
        Scene.objects.create(
            project=self.project,
            title="С заметкой",
            order=1,
            notes="Режиссёрская ремарка",
        )
        Scene.objects.create(
            project=self.project,
            title="Пустая",
            order=2,
            notes="  \n ",
        )

        snapshot = calculate_project_progress(self.project)

        self.assertEqual(snapshot.script, Fraction(1, 2))

    def test_template_heading_is_empty_but_custom_heading_is_content(self):
        heading = "ИНТ. ЛОКАЦИЯ — ДЕНЬ"
        Scene.objects.create(
            project=self.project,
            title="Новая сцена",
            order=1,
            script_text=heading,
            script_blocks=[
                {"id": "template", "type": "scene_heading", "text": heading}
            ],
        )
        custom_heading = "НАТ. МАРС — НОЧЬ"
        Scene.objects.create(
            project=self.project,
            title="Марс",
            order=2,
            script_text=custom_heading,
            script_blocks=[
                {
                    "id": "custom",
                    "type": "scene_heading",
                    "text": custom_heading,
                }
            ],
        )

        snapshot = calculate_project_progress(self.project)

        self.assertEqual(snapshot.script, Fraction(1, 2))

    def test_project_deletion_removes_progress_sources_with_their_assets(self):
        scene = Scene.objects.create(
            project=self.project,
            title="Сцена",
            order=1,
        )
        storyboard_asset = _asset(
            self.project,
            AssetType.STORYBOARD,
            "storyboard-delete.png",
        )
        video_asset = _asset(
            self.project,
            AssetType.VIDEO,
            "video-delete.mp4",
        )
        SceneStoryboard.objects.create(
            scene=scene,
            asset=storyboard_asset,
            source_scene_version=scene.version,
        )
        VideoShot.objects.create(
            project=self.project,
            scene=scene,
            order=1,
            final_asset=video_asset,
        )

        self.project.delete()

        self.assertFalse(SceneStoryboard.objects.exists())
        self.assertFalse(VideoShot.objects.exists())
        self.assertFalse(ProjectAsset.objects.exists())


class ProjectProgressLifecycleApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user("progress-api-owner")
        self.token = str(UserKey.objects.create(user=self.owner).key)
        self.project = _project(self.owner, "Progress API")
        self.scene = Scene.objects.create(
            project=self.project,
            title="Сцена",
            order=1,
            description="Контент",
        )

    def _headers(self, token=None):
        return {"HTTP_X_USER_TOKEN": token or self.token}

    def test_storyboard_becomes_stale_and_requires_current_revision_confirmation(self):
        storyboard_asset = _asset(
            self.project,
            AssetType.STORYBOARD,
            "storyboard.png",
        )
        storyboard_url = (
            f"/api/projects/{self.project.id}/scenes/{self.scene.id}/storyboard/"
        )
        response = self.client.put(
            storyboard_url,
            {
                "assetId": storyboard_asset.id,
                "sourceSceneVersion": self.scene.version,
            },
            format="json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["needsReview"])

        Scene.objects.filter(pk=self.scene.pk).update(version=2)
        dashboard_url = f"/api/projects/{self.project.id}/dashboard/"
        dashboard = self.client.get(dashboard_url, **self._headers()).json()
        progress = dashboard["progress"]["readiness"]
        self.assertEqual(progress["storyboard"], 0.0)
        self.assertEqual(progress["storyboardNeedsReview"], 1)
        self.assertEqual(dashboard["pipeline"]["storyboard"]["subtitle"], "0 сцен")

        confirm_url = f"{storyboard_url}confirm/"
        conflict = self.client.post(
            confirm_url,
            {"expectedSceneVersion": 1},
            format="json",
            **self._headers(),
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["currentVersion"], 2)

        confirmed = self.client.post(
            confirm_url,
            {"expectedSceneVersion": 2},
            format="json",
            **self._headers(),
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertFalse(confirmed.json()["needsReview"])
        dashboard = self.client.get(dashboard_url, **self._headers()).json()
        progress = dashboard["progress"]["readiness"]
        self.assertEqual(progress["storyboard"], 1.0)
        self.assertEqual(progress["storyboardNeedsReview"], 0)
        self.assertEqual(dashboard["pipeline"]["storyboard"]["subtitle"], "1 сцена")

    def test_video_progress_tracks_final_selection_not_generation_attempts(self):
        shots_url = f"/api/projects/{self.project.id}/video-shots/"
        created = self.client.post(
            shots_url,
            {"sceneId": self.scene.id, "title": "Общий план"},
            format="json",
            **self._headers(),
        )
        self.assertEqual(created.status_code, 201)
        shot = created.json()
        first_attempt = _asset(self.project, AssetType.VIDEO, "attempt-1.mp4")
        _asset(self.project, AssetType.VIDEO, "attempt-2.mp4")
        dashboard_url = f"/api/projects/{self.project.id}/dashboard/"
        dashboard = self.client.get(dashboard_url, **self._headers()).json()
        self.assertEqual(dashboard["progress"]["readiness"]["video"], 0.0)
        self.assertEqual(dashboard["pipeline"]["video"]["subtitle"], "0 шотов")

        selected = self.client.patch(
            f"{shots_url}{shot['id']}/",
            {"version": shot["version"], "finalAssetId": first_attempt.id},
            format="json",
            **self._headers(),
        )
        self.assertEqual(selected.status_code, 200)
        dashboard = self.client.get(dashboard_url, **self._headers()).json()
        self.assertEqual(dashboard["progress"]["readiness"]["video"], 1.0)
        self.assertEqual(dashboard["pipeline"]["video"]["subtitle"], "1 шот")

        unselected = self.client.patch(
            f"{shots_url}{shot['id']}/",
            {"version": selected.json()["version"], "finalAssetId": None},
            format="json",
            **self._headers(),
        )
        self.assertEqual(unselected.status_code, 200)
        dashboard = self.client.get(dashboard_url, **self._headers()).json()
        self.assertEqual(dashboard["progress"]["readiness"]["video"], 0.0)
        self.assertEqual(dashboard["pipeline"]["video"]["subtitle"], "0 шотов")

    def test_lifecycle_mutations_require_edit_access_and_project_assets(self):
        viewer = User.objects.create_user("progress-api-viewer")
        viewer_token = str(UserKey.objects.create(user=viewer).key)
        ProjectMember.objects.create(
            project=self.project,
            user=viewer,
            role=ProjectMemberRole.VIEWER,
        )
        local_storyboard = _asset(
            self.project,
            AssetType.STORYBOARD,
            "local-storyboard.png",
        )
        storyboard_url = (
            f"/api/projects/{self.project.id}/scenes/{self.scene.id}/storyboard/"
        )

        forbidden_storyboard = self.client.put(
            storyboard_url,
            {
                "assetId": local_storyboard.id,
                "sourceSceneVersion": self.scene.version,
            },
            format="json",
            **self._headers(viewer_token),
        )
        forbidden_shot = self.client.post(
            f"/api/projects/{self.project.id}/video-shots/",
            {"sceneId": self.scene.id},
            format="json",
            **self._headers(viewer_token),
        )
        self.assertEqual(forbidden_storyboard.status_code, 403)
        self.assertEqual(forbidden_shot.status_code, 403)

        foreign_owner = User.objects.create_user("progress-api-foreign-owner")
        foreign_project = _project(foreign_owner, "Foreign Progress")
        foreign_storyboard = _asset(
            foreign_project,
            AssetType.STORYBOARD,
            "foreign-storyboard.png",
        )
        foreign_video = _asset(
            foreign_project,
            AssetType.VIDEO,
            "foreign-video.mp4",
        )
        rejected_storyboard = self.client.put(
            storyboard_url,
            {
                "assetId": foreign_storyboard.id,
                "sourceSceneVersion": self.scene.version,
            },
            format="json",
            **self._headers(),
        )
        self.assertEqual(rejected_storyboard.status_code, 404)

        shots_url = f"/api/projects/{self.project.id}/video-shots/"
        shot = self.client.post(
            shots_url,
            {"sceneId": self.scene.id},
            format="json",
            **self._headers(),
        ).json()
        rejected_video = self.client.patch(
            f"{shots_url}{shot['id']}/",
            {"version": shot["version"], "finalAssetId": foreign_video.id},
            format="json",
            **self._headers(),
        )
        self.assertEqual(rejected_video.status_code, 400)

    def test_linked_progress_assets_explain_why_deletion_is_blocked(self):
        storyboard_asset = _asset(
            self.project,
            AssetType.STORYBOARD,
            "linked-storyboard.png",
        )
        SceneStoryboard.objects.create(
            scene=self.scene,
            asset=storyboard_asset,
            source_scene_version=self.scene.version,
        )
        final_video = _asset(
            self.project,
            AssetType.VIDEO,
            "linked-final.mp4",
        )
        VideoShot.objects.create(
            project=self.project,
            scene=self.scene,
            order=1,
            final_asset=final_video,
        )

        for asset in (storyboard_asset, final_video):
            response = self.client.delete(
                f"/api/projects/{self.project.id}/assets/{asset.id}/",
                **self._headers(),
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["code"],
                "PROJECT_PROGRESS_ASSET_IN_USE",
            )
