from __future__ import annotations

import uuid

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework import status

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import CharacterStatus, StudioCharacter
from w_craft_back.character_studio.tree_models import ItemFolder, MenuFolder
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project


class CharacterTreeApiTests(TestCase):
    folder_id = uuid.UUID("00000000-0000-4000-8000-000000000501")
    item_id = uuid.UUID("00000000-0000-4000-8000-000000000502")

    def setUp(self) -> None:
        self.owner, self.owner_key = self._user("tree-owner")
        self.editor, self.editor_key = self._user("tree-editor")
        self.viewer, self.viewer_key = self._user("tree-viewer")
        self.outsider, self.outsider_key = self._user("tree-outsider")
        self.other_owner, self.other_owner_key = self._user("tree-other-owner")
        self.project = self._project(self.owner, self.owner_key, "Tree")
        self.other_project = self._project(
            self.other_owner,
            self.other_owner_key,
            "Other",
        )
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
            name="Mira",
            status=CharacterStatus.ACTIVE,
        )
        self.other_character = StudioCharacter.objects.create(
            project=self.other_project,
            user=self.other_owner_key,
            name="Other",
            status=CharacterStatus.ACTIVE,
        )

    @staticmethod
    def _user(username: str) -> tuple[User, UserKey]:
        user = User.objects.create_user(username=username, password="password")
        return user, UserKey.objects.create(user=user)

    @staticmethod
    def _project(owner: User, owner_key: UserKey, title: str) -> Project:
        return Project.objects.create(
            owner=owner,
            title=title,
            format="series",
            annotation="Short",
            synopsis="Long",
        )

    @staticmethod
    def _headers(user_key: UserKey) -> dict[str, str]:
        return {"HTTP_X_USER_TOKEN": str(user_key.key)}

    def _tree_url(self, project: Project | None = None) -> str:
        project = project or self.project
        return f"/api/projects/{project.id}/character-tree/"

    def _nodes_url(self, project: Project | None = None) -> str:
        return f"{self._tree_url(project)}nodes/"

    def _node_url(
        self,
        node_id: uuid.UUID,
        project: Project | None = None,
    ) -> str:
        return f"{self._nodes_url(project)}{node_id}/"

    def test_authentication_roles_and_project_isolation(self) -> None:
        self.assertEqual(
            self.client.get(self._tree_url()).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        viewer_headers = self._headers(self.viewer_key)
        self.assertEqual(
            self.client.get(self._tree_url(), **viewer_headers).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.post(
                self._nodes_url(),
                {
                    "id": str(self.folder_id),
                    "name": "Viewer folder",
                    "type": "folder",
                },
                content_type="application/json",
                **viewer_headers,
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        outsider_headers = self._headers(self.outsider_key)
        self.assertEqual(
            self.client.get(self._tree_url(), **outsider_headers).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_editor_creates_folder_placeholder_and_link(self) -> None:
        headers = self._headers(self.editor_key)
        folder_response = self.client.post(
            self._nodes_url(),
            {
                "id": str(self.folder_id),
                "name": "Cast",
                "type": "folder",
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(folder_response.status_code, status.HTTP_201_CREATED)

        placeholder_response = self.client.post(
            self._nodes_url(),
            {
                "id": str(self.item_id),
                "name": "Unfinished",
                "type": "character",
                "parent_id": str(self.folder_id),
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(placeholder_response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(placeholder_response.json()["character_id"])
        self.assertEqual(
            self.client.get(self._tree_url(), **headers).json()[0]["children"],
            [],
        )

        link_response = self.client.post(
            self._nodes_url(),
            {
                "id": str(self.item_id),
                "name": self.character.name,
                "type": "character",
                "studio_character_id": str(self.character.character_id),
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(link_response.status_code, status.HTTP_201_CREATED)
        replay_response = self.client.post(
            self._nodes_url(),
            {
                "id": str(self.item_id),
                "name": self.character.name,
                "type": "character",
                "studio_character_id": str(self.character.character_id),
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(replay_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(replay_response.json(), link_response.json())

        tree = self.client.get(self._tree_url(), **headers).json()
        child = tree[0]["children"][0]
        self.assertEqual(child["id"], str(self.item_id))
        self.assertEqual(child["key"], str(self.character.character_id))
        self.assertEqual(child["character_id"], str(self.character.character_id))
        self.assertNotIn("legacy_hero_id", child)

    def test_malformed_json_returns_bad_request(self) -> None:
        headers = self._headers(self.editor_key)
        create_response = self.client.generic(
            "POST",
            self._nodes_url(),
            data=b'{"id":',
            content_type="application/json",
            **headers,
        )
        folder = MenuFolder.objects.create(
            key=self.folder_id,
            name="Cast",
            cur_project=self.project,
            is_folder=True,
        )
        rename_response = self.client.generic(
            "PATCH",
            self._node_url(folder.key),
            data=b'{"name":',
            content_type="application/json",
            **headers,
        )

        self.assertEqual(create_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(rename_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_renames_placement_and_studio_character(self) -> None:
        item = ItemFolder.objects.create(
            key=self.item_id,
            name=self.character.name,
            cur_project=self.project,
            is_folder=False,
            studio_character=self.character,
        )

        response = self.client.patch(
            self._node_url(self.item_id),
            {"name": "Renamed"},
            content_type="application/json",
            **self._headers(self.editor_key),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.character.refresh_from_db()
        self.assertEqual(item.name, "Renamed")
        self.assertEqual(self.character.name, "Renamed")

    def test_create_rejects_second_placement_for_same_character(self) -> None:
        headers = self._headers(self.editor_key)
        placeholder_id = uuid.uuid4()
        payload = {
            "name": self.character.name,
            "type": "character",
            "studio_character_id": str(self.character.character_id),
        }
        first_response = self.client.post(
            self._nodes_url(),
            {**payload, "id": str(self.item_id)},
            content_type="application/json",
            **headers,
        )
        second_response = self.client.post(
            self._nodes_url(),
            {**payload, "id": str(uuid.uuid4())},
            content_type="application/json",
            **headers,
        )
        placeholder_response = self.client.post(
            self._nodes_url(),
            {
                "id": str(placeholder_id),
                "name": "Placeholder",
                "type": "character",
            },
            content_type="application/json",
            **headers,
        )
        completion_response = self.client.post(
            self._nodes_url(),
            {**payload, "id": str(placeholder_id)},
            content_type="application/json",
            **headers,
        )

        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            placeholder_response.status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            completion_response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            ItemFolder.objects.filter(studio_character=self.character).count(),
            1,
        )

    def test_delete_subtree_preserves_linked_studio_character(self) -> None:
        folder = MenuFolder.objects.create(
            key=self.folder_id,
            name="Cast",
            cur_project=self.project,
            is_folder=True,
        )
        ItemFolder.objects.create(
            key=self.item_id,
            name=self.character.name,
            parent=folder,
            cur_project=self.project,
            is_folder=False,
            studio_character=self.character,
        )

        response = self.client.delete(
            self._node_url(self.folder_id),
            **self._headers(self.owner_key),
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            MenuFolder.objects.filter(cur_project=self.project).exists()
        )
        self.assertTrue(
            StudioCharacter.objects.filter(pk=self.character.pk).exists()
        )

    def test_create_rejects_cross_project_links_and_body_project(self) -> None:
        foreign_folder = MenuFolder.objects.create(
            name="Foreign",
            cur_project=self.other_project,
            is_folder=True,
        )
        headers = self._headers(self.owner_key)
        for payload in (
            {
                "id": str(uuid.uuid4()),
                "name": "Foreign parent",
                "type": "character",
                "parent_id": str(foreign_folder.key),
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Foreign character",
                "type": "character",
                "studio_character_id": str(self.other_character.character_id),
            },
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.client.post(
                        self._nodes_url(),
                        payload,
                        content_type="application/json",
                        **headers,
                    ).status_code,
                    status.HTTP_404_NOT_FOUND,
                )

        response = self.client.post(
            self._nodes_url(),
            {
                "id": str(uuid.uuid4()),
                "name": "Body project",
                "type": "folder",
                "projectId": self.other_project.id,
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_legacy_character_routes_are_removed(self) -> None:
        headers = self._headers(self.owner_key)
        for legacy_url in (
            "/api/character/select/",
            "/api/character/create/",
            "/api/character/rename/",
            "/api/character/delete/",
        ):
            with self.subTest(url=legacy_url):
                self.assertEqual(
                    self.client.get(legacy_url, **headers).status_code,
                    status.HTTP_404_NOT_FOUND,
                )
