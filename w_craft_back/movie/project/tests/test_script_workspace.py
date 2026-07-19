"""API coverage for the unified cards/script workspace."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterRole,
    CharacterStatus,
    StudioCharacter,
)
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
    Scene,
)
from w_craft_back.movie.project.models import Project


def make_user(username: str) -> tuple[User, UserKey]:
    user = User.objects.create_user(username=username, password="pw")
    return user, UserKey.objects.create(user=user)


def make_project(owner: User, owner_key: UserKey, title: str) -> Project:
    project = Project.objects.create(
        user=owner_key,
        owner=owner,
        title=title,
        format="short_film",
        annot="",
        desc="",
    )
    ProjectMember.objects.create(
        project=project,
        user=owner,
        role=ProjectMemberRole.OWNER,
    )
    return project


class ScriptWorkspaceApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_key = make_user("script-owner")
        self.editor, self.editor_key = make_user("script-editor")
        self.viewer, self.viewer_key = make_user("script-viewer")
        self.outsider, self.outsider_key = make_user("script-outsider")
        self.project = make_project(self.owner, self.owner_key, "Movie")
        ProjectMember.objects.create(
            project=self.project,
            user=self.editor,
            role=ProjectMemberRole.EDITOR,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.viewer,
            role=ProjectMemberRole.VIEWER,
        )
        self.character = StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Anna",
            role=CharacterRole.MAIN,
            status=CharacterStatus.ACTIVE,
            personality={"traits": ["brave"]},
            backstory="A long road home",
            speech_style="Short sentences",
        )

    def tearDown(self):
        cache.clear()
        super().tearDown()

    @property
    def scenes_url(self) -> str:
        return f"/api/projects/{self.project.id}/scenes/"

    @property
    def characters_url(self) -> str:
        return f"/api/projects/{self.project.id}/characters/"

    def token(self, key: UserKey) -> dict:
        return {"HTTP_X_USER_TOKEN": str(key.key)}

    def create_scene(self) -> dict:
        response = self.client.post(
            self.scenes_url,
            data={
                "title": "Cafe",
                "description": "A dangerous conversation",
                "script_blocks": [
                    {
                        "id": "heading-1",
                        "type": "scene_heading",
                        "text": "INT. CAFE - NIGHT",
                    },
                    {
                        "id": "dialogue-1",
                        "type": "dialogue",
                        "text": "We need to leave.",
                        "characterId": str(self.character.character_id),
                    },
                    {"id": "note-1", "type": "note", "text": ""},
                ],
                "character_ids": [str(self.character.character_id)],
                "act": 2,
                "duration_seconds": 420,
                "mood": "tense",
                "scene_type": "turn",
                "notes": "Keep the pauses",
            },
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_scene_collection_access_and_viewer_permissions(self):
        self.assertEqual(self.client.get(self.scenes_url).status_code, 401)
        self.assertEqual(
            self.client.get(
                self.scenes_url,
                **self.token(self.outsider_key),
            ).status_code,
            403,
        )
        response = self.client.get(
            self.scenes_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["project"]["permissions"]["canEdit"])
        self.assertEqual(
            self.client.post(
                self.scenes_url,
                {"title": "Forbidden"},
                format="json",
                **self.token(self.viewer_key),
            ).status_code,
            403,
        )

    def test_create_list_detail_and_delete_scene(self):
        created = self.create_scene()
        self.assertEqual(created["scriptText"], "INT. CAFE - NIGHT\nWe need to leave.")
        self.assertEqual(created["characters"][0]["id"], str(self.character.character_id))
        self.assertEqual(created["act"], 2)
        scene = Scene.objects.get(pk=created["id"])
        self.assertEqual(scene.script_text, created["scriptText"])

        collection = self.client.get(
            self.scenes_url,
            **self.token(self.viewer_key),
        ).json()
        self.assertEqual(collection["stats"]["sceneCount"], 1)
        self.assertEqual(collection["stats"]["totalDurationSeconds"], 420)
        self.assertEqual(collection["stats"]["acts"][1]["sceneCount"], 1)
        self.assertEqual(collection["stats"]["acts"][1]["durationSeconds"], 420)
        self.assertNotIn("totalDurationSeconds", collection["stats"]["acts"][1])
        self.assertEqual(collection["scenes"][0]["scriptBlocks"], created["scriptBlocks"])

        detail_url = f"{self.scenes_url}{scene.id}/"
        detail = self.client.get(
            detail_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["id"], scene.id)
        self.assertEqual(detail.json()["cameraSettings"], {})
        self.assertEqual(detail.json()["updatedById"], self.editor.id)
        self.assertEqual(detail.json()["updatedByUsername"], self.editor.username)

        deleted = self.client.delete(detail_url, **self.token(self.editor_key))
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(Scene.objects.filter(pk=scene.id).exists())

    def test_patch_requires_version_and_enforces_optimistic_lock(self):
        created = self.create_scene()
        detail_url = f"{self.scenes_url}{created['id']}/"
        missing_version = self.client.patch(
            detail_url,
            {"title": "No version"},
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(missing_version.status_code, 400)
        self.assertIn("version", missing_version.json()["errors"])

        updated = self.client.patch(
            detail_url,
            {
                "version": created["version"],
                "script_blocks": [
                    {"id": "action-2", "type": "action", "text": "Door opens."}
                ],
                "duration_seconds": 60,
            },
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(updated.json()["version"], created["version"] + 1)
        self.assertEqual(updated.json()["scriptText"], "Door opens.")

        text_only = self.client.patch(
            detail_url,
            {
                "version": updated.json()["version"],
                "script_text": "Legacy editor text",
            },
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(text_only.status_code, 200, text_only.content)
        self.assertEqual(text_only.json()["scriptText"], "Legacy editor text")
        self.assertEqual(text_only.json()["scriptBlocks"][0]["type"], "action")
        scene = Scene.objects.get(pk=created["id"])
        self.assertEqual(scene.script_blocks, [])

        conflict = self.client.patch(
            detail_url,
            {"version": created["version"], "title": "Stale"},
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "VERSION_CONFLICT")

    def test_legacy_script_text_serializes_as_fallback_block(self):
        scene = Scene.objects.create(
            project=self.project,
            title="Legacy",
            script_text="Legacy action",
            script_blocks=[],
            camera_settings={"shot": "wide"},
            updated_by=self.owner,
        )
        response = self.client.get(
            f"{self.scenes_url}{scene.id}/",
            **self.token(self.viewer_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cameraSettings"], {"shot": "wide"})
        self.assertEqual(response.json()["updatedById"], self.owner.id)
        self.assertEqual(response.json()["updatedByUsername"], self.owner.username)
        self.assertEqual(response.json()["scriptBlocks"][0]["type"], "action")
        scene.refresh_from_db()
        self.assertEqual(scene.script_blocks, [])

    def test_block_validation_and_foreign_project_character_rejection(self):
        bad_block = self.client.post(
            self.scenes_url,
            {
                "title": "Bad block",
                "script_blocks": [{"id": "", "type": "unknown", "text": "x"}],
            },
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(bad_block.status_code, 400)

        other_project = make_project(self.outsider, self.outsider_key, "Other")
        foreign = StudioCharacter.objects.create(
            project=other_project,
            user=self.outsider_key,
            name="Foreign",
            status=CharacterStatus.ACTIVE,
        )
        response = self.client.post(
            self.scenes_url,
            {
                "title": "Cross-project",
                "character_ids": [str(foreign.character_id)],
            },
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Scene.objects.filter(title="Cross-project").exists())

    def test_characters_get_visibility_counts_and_quick_create(self):
        draft = StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Hidden draft",
            status=CharacterStatus.DRAFT,
        )
        created = self.create_scene()
        response = self.client.get(
            self.characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(response.status_code, 200)
        characters = response.json()["characters"]
        self.assertEqual([character["name"] for character in characters], ["Anna"])
        self.assertEqual(characters[0]["sceneCount"], 1)
        self.assertEqual(characters[0]["sceneIds"], [created["id"]])
        self.assertEqual(characters[0]["personality"], {"traits": ["brave"]})
        self.assertNotIn(str(draft.character_id), [item["id"] for item in characters])

        quick = self.client.post(
            self.characters_url,
            {"name": "Quick"},
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(quick.status_code, 201, quick.content)
        character = StudioCharacter.objects.get(character_id=quick.json()["id"])
        self.assertEqual(character.status, CharacterStatus.ACTIVE)
        visible_names = [
            item["name"]
            for item in self.client.get(
                self.characters_url,
                **self.token(self.viewer_key),
            ).json()["characters"]
        ]
        self.assertIn("Quick", visible_names)
