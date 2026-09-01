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
        owner=owner,
        title=title,
        format="short_film",
        annotation="",
        synopsis="",
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

    @property
    def missing_characters_url(self) -> str:
        return f"/api/projects/{self.project.id}/scenes/missing-characters/"

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
        self.assertEqual(
            created["characters"][0]["id"],
            str(self.character.character_id),
        )
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
        self.assertEqual(
            collection["scenes"][0]["scriptBlocks"],
            created["scriptBlocks"],
        )

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

    def test_reorder_scenes_updates_order_and_acts_atomically(self):
        first_payload = self.create_scene()
        first = Scene.objects.get(pk=first_payload["id"])
        second = Scene.objects.create(
            project=self.project,
            title="Hall",
            order=2,
            act=1,
            updated_by=self.owner,
        )
        third = Scene.objects.create(
            project=self.project,
            title="Street",
            order=3,
            act=3,
            updated_by=self.owner,
        )

        response = self.client.patch(
            f"{self.scenes_url}reorder/",
            {
                "scenes": [
                    {
                        "id": second.id,
                        "order": 1,
                        "act": 1,
                        "version": second.version,
                    },
                    {
                        "id": first.id,
                        "order": 2,
                        "act": 2,
                        "version": first.version,
                    },
                    {
                        "id": third.id,
                        "order": 3,
                        "act": 3,
                        "version": third.version,
                    },
                ]
            },
            format="json",
            **self.token(self.editor_key),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [
                (item["id"], item["order"], item["act"])
                for item in response.json()["scenes"]
            ],
            [(second.id, 1, 1), (first.id, 2, 2), (third.id, 3, 3)],
        )
        first.refresh_from_db()
        second.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(first.version, first_payload["version"] + 1)
        self.assertEqual(second.version, 2)
        self.assertEqual(third.version, 1)
        self.assertEqual(second.updated_by, self.editor)

    def test_reorder_scenes_rejects_invalid_or_stale_payload_without_changes(self):
        first_payload = self.create_scene()
        first = Scene.objects.get(pk=first_payload["id"])
        second = Scene.objects.create(
            project=self.project,
            title="Hall",
            order=2,
            updated_by=self.owner,
        )
        reorder_url = f"{self.scenes_url}reorder/"

        duplicate_order = self.client.patch(
            reorder_url,
            {
                "scenes": [
                    {"id": first.id, "order": 1, "act": 1, "version": first.version},
                    {"id": second.id, "order": 1, "act": 1, "version": second.version},
                ]
            },
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(duplicate_order.status_code, 400)

        incomplete = self.client.patch(
            reorder_url,
            {
                "scenes": [
                    {
                        "id": first.id,
                        "order": 1,
                        "act": 1,
                        "version": first.version,
                    }
                ]
            },
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(incomplete.status_code, 400)

        stale = self.client.patch(
            reorder_url,
            {
                "scenes": [
                    {"id": second.id, "order": 1, "act": 1, "version": 999},
                    {"id": first.id, "order": 2, "act": 2, "version": first.version},
                ]
            },
            format="json",
            **self.token(self.editor_key),
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "VERSION_CONFLICT")
        self.assertEqual(
            list(
                Scene.objects.filter(project=self.project)
                .order_by("order")
                .values_list("id", "order")
            ),
            [(first.id, 1), (second.id, 2)],
        )

        forbidden = self.client.patch(
            reorder_url,
            {
                "scenes": [
                    {
                        "id": first.id,
                        "order": 1,
                        "act": 1,
                        "version": first.version,
                    },
                    {
                        "id": second.id,
                        "order": 2,
                        "act": 1,
                        "version": second.version,
                    },
                ]
            },
            format="json",
            **self.token(self.viewer_key),
        )
        self.assertEqual(forbidden.status_code, 403)

        unauthorized = self.client.patch(
            reorder_url,
            {
                "scenes": [
                    {
                        "id": first.id,
                        "order": 1,
                        "act": 1,
                        "version": first.version,
                    },
                    {
                        "id": second.id,
                        "order": 2,
                        "act": 1,
                        "version": second.version,
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(unauthorized.status_code, 401)

    def test_reorder_uses_safe_temporary_orders_for_legacy_large_values(self):
        first_payload = self.create_scene()
        first = Scene.objects.get(pk=first_payload["id"])
        Scene.objects.filter(pk=first.id).update(order=2_147_483_647)
        first.refresh_from_db()
        second = Scene.objects.create(
            project=self.project,
            title="Hall",
            order=2,
            updated_by=self.owner,
        )

        response = self.client.patch(
            f"{self.scenes_url}reorder/",
            {
                "scenes": [
                    {
                        "id": second.id,
                        "order": 1,
                        "act": 1,
                        "version": second.version,
                    },
                    {
                        "id": first.id,
                        "order": 2,
                        "act": 2,
                        "version": first.version,
                    },
                ]
            },
            format="json",
            **self.token(self.editor_key),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            list(
                Scene.objects.filter(project=self.project)
                .order_by("order")
                .values_list("id", "order")
            ),
            [(second.id, 1), (first.id, 2)],
        )

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

    def test_missing_characters_access_uses_project_view_permission(self):
        self.assertEqual(self.client.get(self.missing_characters_url).status_code, 401)
        self.assertEqual(
            self.client.get(
                self.missing_characters_url,
                **self.token(self.outsider_key),
            ).status_code,
            403,
        )
        response = self.client.get(
            self.missing_characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"characters": []})

    def test_missing_characters_dialogue_boundary_and_automatic_refresh(self):
        blocks = [
            {"id": "mira", "type": "character", "text": "  Ｍira\t Ivanova  "},
            *[
                {"id": f"line-{index}", "type": "dialogue", "text": "Line"}
                for index in range(4)
            ],
            {"id": "remark", "type": "remark", "text": "quietly"},
            {"id": "line-5", "type": "dialogue", "text": "Fifth line"},
            {"id": "reset", "type": "action", "text": "Mira leaves"},
            {"id": "orphan", "type": "dialogue", "text": "Not Mira's line"},
        ]
        scene = Scene.objects.create(
            project=self.project,
            title="Boundary",
            script_blocks=blocks,
            updated_by=self.owner,
        )

        first = self.client.get(
            self.missing_characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"characters": []})

        draft = StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Mira Ivanova",
            role=CharacterRole.MAIN,
            status=CharacterStatus.DRAFT,
        )
        draft_is_significant = self.client.get(
            self.missing_characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(
            draft_is_significant.json(),
            {
                "characters": [
                    {"name": "Mira Ivanova", "dialogueCount": 5, "sceneCount": 1}
                ]
            },
        )

        scene.script_blocks = [
            *blocks,
            {"id": "other", "type": "character", "text": "Other"},
            {
                "id": "line-6",
                "type": "dialogue",
                "text": "Explicit Mira line",
                "characterId": str(draft.character_id),
            },
        ]
        scene.save(update_fields=["script_blocks"])
        refreshed = self.client.get(
            self.missing_characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(
            refreshed.json(),
            {
                "characters": [
                    {"name": "Mira Ivanova", "dialogueCount": 6, "sceneCount": 1}
                ]
            },
        )

        draft.status = CharacterStatus.ACTIVE
        draft.save(update_fields=["status"])
        resolved = self.client.get(
            self.missing_characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(resolved.json(), {"characters": []})

    def test_missing_character_in_two_scenes_is_significant_and_names_collapse(self):
        Scene.objects.create(
            project=self.project,
            title="First ghost scene",
            order=1,
            script_blocks=[
                {"id": "ghost-1", "type": "character", "text": "  GHOST   VOICE "}
            ],
            updated_by=self.owner,
        )
        Scene.objects.create(
            project=self.project,
            title="Second ghost scene",
            order=2,
            script_blocks=[
                {"id": "ghost-2", "type": "character", "text": "ghost voice"}
            ],
            updated_by=self.owner,
        )

        response = self.client.get(
            self.missing_characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(
            response.json(),
            {
                "characters": [
                    {"name": "GHOST VOICE", "dialogueCount": 0, "sceneCount": 2}
                ]
            },
        )

    def test_visible_character_name_matches_resolve_without_scene_links(self):
        StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Mira Ivanova",
            status=CharacterStatus.ACTIVE,
        )
        StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Clara",
            status=CharacterStatus.REFERENCES_LOCKED,
        )
        StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Mira Ivanova",
            status=CharacterStatus.DRAFT,
        )
        for order in (1, 2):
            Scene.objects.create(
                project=self.project,
                title=f"Matched {order}",
                order=order,
                script_blocks=[
                    {
                        "id": f"mira-{order}",
                        "type": "character",
                        "text": " ＭIRA   IVANOVA ",
                    },
                    {
                        "id": f"clara-{order}",
                        "type": "character",
                        "text": "CLARA",
                    },
                ],
                updated_by=self.owner,
            )

        response = self.client.get(
            self.missing_characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(response.json(), {"characters": []})

    def test_episodic_and_cameo_matches_are_excluded(self):
        episodic = StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Courier",
            role=CharacterRole.EPISODIC,
            status=CharacterStatus.DRAFT,
        )
        StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Passerby",
            role=CharacterRole.CAMEO,
            status=CharacterStatus.DRAFT,
        )
        Scene.objects.create(
            project=self.project,
            title="Minor roles",
            script_blocks=[
                {
                    "id": "courier",
                    "type": "character",
                    "text": "Courier",
                    "characterId": str(episodic.character_id),
                },
                *[
                    {"id": f"courier-{index}", "type": "dialogue", "text": "Line"}
                    for index in range(6)
                ],
                {"id": "reset", "type": "action", "text": "The courier leaves"},
                {"id": "passerby", "type": "character", "text": "passerby"},
                *[
                    {"id": f"passerby-{index}", "type": "dialogue", "text": "Line"}
                    for index in range(6)
                ],
            ],
            updated_by=self.owner,
        )

        response = self.client.get(
            self.missing_characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(response.json(), {"characters": []})

    def test_ambiguous_duplicate_names_do_not_resolve_missing_character(self):
        for status_value in (
            CharacterStatus.ACTIVE,
            CharacterStatus.REFERENCES_LOCKED,
        ):
            StudioCharacter.objects.create(
                project=self.project,
                user=self.owner_key,
                name="Duplicate",
                status=status_value,
            )
        Scene.objects.create(
            project=self.project,
            title="Ambiguous first",
            order=1,
            script_blocks=[
                {"id": "duplicate-1", "type": "character", "text": "Duplicate"}
            ],
            updated_by=self.owner,
        )
        Scene.objects.create(
            project=self.project,
            title="Ambiguous second",
            order=2,
            script_blocks=[
                {"id": "duplicate-2", "type": "character", "text": "duplicate"}
            ],
            updated_by=self.owner,
        )

        response = self.client.get(
            self.missing_characters_url,
            **self.token(self.viewer_key),
        )
        self.assertEqual(
            response.json(),
            {
                "characters": [
                    {"name": "Duplicate", "dialogueCount": 0, "sceneCount": 2}
                ]
            },
        )
