"""Tests for the project dashboard API."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.music.models import MusicAsset, MusicTrackVersion
from w_craft_back.movie.project.dashboard_models import (
    ActivityType,
    Location,
    MusicTrack,
    ProjectActivity,
    ProjectMember,
    ProjectMemberRole,
    ProjectProgress,
    ProjectTag,
    Scene,
    SceneMusic,
)
from w_craft_back.movie.project.models import Project, ProjectStatus
from w_craft_back.movie.project.services import _music_payload


def _make_user(username: str) -> tuple[User, str]:
    user = User.objects.create_user(username=username, password="pw")
    key = UserKey.objects.create(user=user)
    return user, str(key.key)


def _make_project(owner: User, *, title: str = "Demo") -> Project:
    legacy_key, _ = UserKey.objects.get_or_create(user=owner)
    project = Project.objects.create(
        user=legacy_key,
        owner=owner,
        title=title,
        format="full-movie",
        annot="",
        desc="legacy desc",
        description="dashboard description",
        status=ProjectStatus.IN_PROGRESS,
        is_favorite=True,
    )
    ProjectMember.objects.create(project=project, user=owner, role=ProjectMemberRole.OWNER)
    return project


class DashboardAccessTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_token = _make_user("owner")
        self.viewer, self.viewer_token = _make_user("viewer")
        self.outsider, self.outsider_token = _make_user("outsider")
        self.project = _make_project(self.owner)
        ProjectMember.objects.create(
            project=self.project, user=self.viewer, role=ProjectMemberRole.VIEWER
        )

    def _url(self):
        return f"/api/projects/{self.project.id}/dashboard/"

    def test_no_token_returns_401(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 401)

    def test_owner_can_read(self):
        resp = self.client.get(self._url(), HTTP_X_USER_TOKEN=self.owner_token)
        self.assertEqual(resp.status_code, 200)

    def test_viewer_can_read(self):
        resp = self.client.get(self._url(), HTTP_X_USER_TOKEN=self.viewer_token)
        self.assertEqual(resp.status_code, 200)

    def test_outsider_gets_403(self):
        resp = self.client.get(self._url(), HTTP_X_USER_TOKEN=self.outsider_token)
        self.assertEqual(resp.status_code, 403)

    def test_missing_project_returns_404(self):
        resp = self.client.get(
            "/api/projects/999999/dashboard/", HTTP_X_USER_TOKEN=self.owner_token
        )
        self.assertEqual(resp.status_code, 404)


class DashboardShapeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.token = _make_user("alice")
        self.project = _make_project(self.owner, title="Cyber City Dawn")

    def _get(self):
        url = f"/api/projects/{self.project.id}/dashboard/"
        return self.client.get(url, HTTP_X_USER_TOKEN=self.token).json()

    def test_top_level_keys(self):
        data = self._get()
        for key in (
            "project",
            "stats",
            "characters",
            "pipeline",
            "music",
            "progress",
            "quickActions",
            "recentActivity",
        ):
            self.assertIn(key, data, f"missing top-level key: {key}")

    def test_progress_defaults_to_zero(self):
        data = self._get()
        for k in ("overall", "script", "visual", "audio", "postproduction"):
            self.assertEqual(data["progress"][k], 0)

    def test_progress_uses_db_values(self):
        ProjectProgress.objects.update_or_create(
            project=self.project,
            defaults={
                "overall_progress": 58,
                "script_progress": 80,
                "visual_progress": 42,
                "audio_progress": 67,
                "postproduction_progress": 30,
            },
        )
        data = self._get()
        self.assertEqual(data["progress"]["overall"], 58)
        self.assertEqual(data["progress"]["script"], 80)

    def test_empty_collections_are_arrays(self):
        data = self._get()
        self.assertEqual(data["characters"], [])
        self.assertEqual(data["music"], [])
        self.assertEqual(data["recentActivity"], [])
        self.assertEqual(data["project"]["tags"], [])

    def test_quick_actions_use_available_creation_routes(self):
        actions = {action["key"]: action for action in self._get()["quickActions"]}

        self.assertNotIn("upload_reference", actions)
        self.assertEqual(
            actions["create_location"],
            {
                "key": "create_location",
                "label": "Создать локацию",
                "url": (
                    f"/project/{self.project.id}/references/create"
                    "?category=location"
                ),
            },
        )
        self.assertEqual(
            actions["create_character"],
            {
                "key": "create_character",
                "label": "Создать персонажа",
                "url": f"/project/{self.project.id}/characters/create",
            },
        )
        self.assertEqual(
            actions["create_track"],
            {
                "key": "create_track",
                "label": "Создать трек",
                "url": f"/project/{self.project.id}/music/create",
            },
        )

    def test_stats_defaults(self):
        data = self._get()
        for k in (
            "charactersTotal",
            "charactersActive",
            "scenesTotal",
            "scenesCompleted",
            "musicTotal",
            "musicUsed",
            "locationsTotal",
            "locationsCreated",
        ):
            self.assertEqual(data["stats"][k], 0)

    def test_pipeline_keys(self):
        data = self._get()
        for k in ("script", "storyboard", "references", "models3d", "video"):
            self.assertIn(k, data["pipeline"])
            self.assertIn("progress", data["pipeline"][k])
            self.assertIn("subtitle", data["pipeline"][k])

    def test_status_label_for_in_progress(self):
        data = self._get()
        self.assertEqual(data["project"]["status"], "in_progress")
        self.assertEqual(data["project"]["statusLabel"], "В работе")

    def test_team_member_owner_present(self):
        data = self._get()
        members = data["project"]["teamMembers"]
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["role"], "owner")

    def test_with_seeded_data(self):
        ProjectTag.objects.create(project=self.project, name="Киберпанк")
        track = MusicTrack.objects.create(
            project=self.project,
            title="Neon Shadows",
            author="SWC",
            duration_seconds=222,
            tags=["Киберпанк"],
        )
        loc = Location.objects.create(project=self.project, name="Ночной рынок")
        scene = Scene.objects.create(
            project=self.project, title="Scene 7", order=7, location=loc, status="completed"
        )
        SceneMusic.objects.create(scene=scene, track=track)
        ProjectActivity.objects.create(
            project=self.project,
            user=self.owner,
            activity_type=ActivityType.SCENE_RENDER_COMPLETED,
            title="Scene 7",
            description="рендер завершён",
        )

        data = self._get()
        self.assertEqual(data["project"]["tags"], ["Киберпанк"])
        self.assertEqual(data["stats"]["scenesTotal"], 1)
        self.assertEqual(data["stats"]["scenesCompleted"], 1)
        self.assertEqual(data["stats"]["musicTotal"], 1)
        self.assertEqual(data["stats"]["musicUsed"], 1)
        self.assertEqual(data["stats"]["locationsTotal"], 1)

        music = data["music"]
        self.assertEqual(len(music), 1)
        self.assertEqual(music[0]["durationLabel"], "03:42")
        self.assertEqual(music[0]["usageCount"], 1)
        self.assertEqual(music[0]["usageLabel"], "Используется в 1 сцене")

        activities = data["recentActivity"]
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["type"], "scene_render_completed")

    def test_music_uses_active_immutable_version_audio(self):
        track = MusicTrack.objects.create(
            project=self.project,
            title="Versioned score",
            duration_seconds=9,
            source="generated",
        )
        asset = MusicAsset.objects.create(
            project=self.project,
            file="tests/music/versioned.wav",
            asset_role="generated",
            origin="legacy",
            original_name="versioned.wav",
            duration_seconds=Decimal("12.500"),
            verification_status="legacy_unverified",
        )
        version = MusicTrackVersion.objects.create(
            track=track,
            version_number=1,
            asset=asset,
        )
        track.active_version = version
        track.save(update_fields=["active_version", "updated_at"])

        data = self._get()

        music = data["music"][0]
        self.assertEqual(music["activeVersionId"], str(version.id))
        self.assertEqual(music["activeVersionNumber"], 1)
        self.assertEqual(music["activeVersion"]["versionId"], str(version.id))
        self.assertEqual(music["activeVersion"]["versionNumber"], 1)
        self.assertEqual(music["activeVersion"]["audioUrl"], music["audioUrl"])
        self.assertEqual(music["durationSeconds"], 12.5)
        self.assertEqual(music["durationLabel"], "00:12")
        self.assertEqual(music["source"], "generated")
        self.assertEqual(music["version"], 1)
        self.assertIsNotNone(music["audioUrl"])
        self.assertIsNotNone(music["audioUrlExpiresAt"])
        self.assertNotIn("tests/music/versioned.wav", music["audioUrl"])

    def test_music_retains_signed_legacy_audio_fallback(self):
        track = MusicTrack.objects.create(
            project=self.project,
            title="Legacy score",
            duration_seconds=31,
        )
        track.audio_file.name = "projects/music/legacy.mp3"
        track.save(update_fields=["audio_file", "updated_at"])

        data = self._get()

        music = data["music"][0]
        self.assertIsNone(music["activeVersionId"])
        self.assertIsNone(music["activeVersionNumber"])
        self.assertIsNone(music["activeVersion"])
        self.assertEqual(music["durationSeconds"], 31.0)
        self.assertIsNotNone(music["audioUrl"])
        self.assertNotIn("projects/music/legacy.mp3", music["audioUrl"])

    def test_music_hides_archived_tracks(self):
        MusicTrack.objects.create(
            project=self.project,
            title="Archived score",
            archived_at=timezone.now(),
        )
        MusicTrack.objects.create(
            project=self.project,
            title="Active score",
        )

        data = self._get()
        self.assertEqual([track["title"] for track in data["music"]], ["Active score"])

    def test_music_payload_fetches_versioned_tracks_in_one_query(self):
        for index in range(3):
            track = MusicTrack.objects.create(
                project=self.project,
                title=f"Track {index}",
            )
            asset = MusicAsset.objects.create(
                project=self.project,
                file=f"tests/music/{index}.wav",
                asset_role="generated",
                origin="legacy",
                original_name=f"{index}.wav",
                duration_seconds=Decimal("10.000"),
                verification_status="legacy_unverified",
            )
            version = MusicTrackVersion.objects.create(
                track=track,
                version_number=1,
                asset=asset,
            )
            track.active_version = version
            track.save(update_fields=["active_version", "updated_at"])

        request = RequestFactory().get("/")
        with self.assertNumQueries(1):
            payload = _music_payload(self.project, request)
            self.assertEqual(len(payload), 3)


class ProjectCrudTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.token = _make_user("creator")

    def test_create_project_creates_owner_member_and_progress(self):
        resp = self.client.post(
            "/api/projects/",
            data={"title": "New Movie", "description": "x", "tags": ["Драма"]},
            format="json",
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        project_id = resp.json()["id"]
        self.assertTrue(
            ProjectMember.objects.filter(
                project_id=project_id, user=self.owner, role=ProjectMemberRole.OWNER
            ).exists()
        )
        self.assertTrue(ProjectProgress.objects.filter(project_id=project_id).exists())
        self.assertEqual(
            list(ProjectTag.objects.filter(project_id=project_id).values_list("name", flat=True)),
            ["Драма"],
        )

    def test_create_character_records_activity(self):
        project = _make_project(self.owner, title="X")
        url = f"/api/projects/{project.id}/characters/"
        resp = self.client.post(
            url, data={"name": "Лира Вэй"}, format="json", HTTP_X_USER_TOKEN=self.token
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(
            ProjectActivity.objects.filter(
                project=project, activity_type=ActivityType.CHARACTER_CREATED
            ).exists()
        )

    def test_create_scene_records_activity(self):
        project = _make_project(self.owner, title="X")
        url = f"/api/projects/{project.id}/scenes/"
        resp = self.client.post(
            url, data={"title": "Opening"}, format="json", HTTP_X_USER_TOKEN=self.token
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(
            ProjectActivity.objects.filter(
                project=project, activity_type=ActivityType.SCENE_CREATED
            ).exists()
        )

    def test_outsider_cannot_create_character(self):
        project = _make_project(self.owner, title="X")
        outsider, outsider_token = _make_user("outsider2")
        url = f"/api/projects/{project.id}/characters/"
        resp = self.client.post(
            url, data={"name": "X"}, format="json", HTTP_X_USER_TOKEN=outsider_token
        )
        self.assertEqual(resp.status_code, 403)

    def test_viewer_cannot_create_character(self):
        project = _make_project(self.owner, title="X")
        viewer, viewer_token = _make_user("viewer2")
        ProjectMember.objects.create(
            project=project, user=viewer, role=ProjectMemberRole.VIEWER
        )
        url = f"/api/projects/{project.id}/characters/"
        resp = self.client.post(
            url, data={"name": "X"}, format="json", HTTP_X_USER_TOKEN=viewer_token
        )
        self.assertEqual(resp.status_code, 403)
