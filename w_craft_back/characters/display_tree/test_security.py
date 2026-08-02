from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework import status

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import CharacterStatus, StudioCharacter
from w_craft_back.characters.creating.models import Character
from w_craft_back.characters.display_tree.models import ItemFolder, MenuFolder
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project


class CharacterTreeSecurityTests(TestCase):
    node_id = "00000000-0000-4000-8000-000000000201"

    def setUp(self):
        self.owner, self.owner_key = self._make_user("tree-owner")
        self.editor, self.editor_key = self._make_user("tree-editor")
        self.viewer, self.viewer_key = self._make_user("tree-viewer")
        self.outsider, self.outsider_key = self._make_user("tree-outsider")
        self.other_owner, self.other_owner_key = self._make_user("other-owner")

        self.project = self._make_project(self.owner_key, "Tree project")
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
            user=self.owner_key,
            project=self.project,
            name="Mira",
            status=CharacterStatus.ACTIVE,
        )
        self.node = ItemFolder.objects.create(
            key=self.node_id,
            name=self.character.name,
            user=self.owner_key,
            cur_project=self.project,
            is_folder=False,
            studio_character=self.character,
        )

        self.other_project = self._make_project(
            self.other_owner_key,
            "Other project",
        )
        self.other_parent = MenuFolder.objects.create(
            key="00000000-0000-4000-8000-000000000202",
            name="Foreign folder",
            user=self.other_owner_key,
            cur_project=self.other_project,
            is_folder=True,
        )
        self.other_character = StudioCharacter.objects.create(
            user=self.other_owner_key,
            project=self.other_project,
            name="Foreign character",
            status=CharacterStatus.ACTIVE,
        )
        self.other_hero = Character.objects.create(
            project=self.other_project,
            first_name="Foreign legacy character",
        )

    @staticmethod
    def _make_user(username: str) -> tuple[User, UserKey]:
        user = User.objects.create_user(username=username, password="password")
        return user, UserKey.objects.create(user=user)

    @staticmethod
    def _make_project(user_key: UserKey, title: str) -> Project:
        return Project.objects.create(
            user=user_key,
            owner=user_key.user,
            title=title,
            format="series",
            annot="Short",
            desc="Long",
        )

    @staticmethod
    def _headers(user_key: UserKey) -> dict[str, str]:
        return {"HTTP_X_USER_TOKEN": str(user_key.key)}

    def test_anonymous_tree_operations_return_401(self):
        calls = {
            "get": lambda: self.client.get(
                "/api/character/select/",
                {"projectId": self.project.id},
            ),
            "create": lambda: self.client.post(
                "/api/character/create/",
                {
                    "id": "00000000-0000-4000-8000-000000000203",
                    "name": "Anonymous",
                    "type": "leaf",
                    "parent": None,
                    "projectId": self.project.id,
                },
                content_type="application/json",
            ),
            "rename": lambda: self.client.post(
                "/api/character/rename/",
                {"id": self.node_id, "name": "Anonymous"},
                content_type="application/json",
            ),
            "delete": lambda: self.client.post(
                "/api/character/delete/",
                {"id": self.node_id},
                content_type="application/json",
            ),
        }

        for operation, call in calls.items():
            with self.subTest(operation=operation):
                self.assertEqual(
                    call().status_code,
                    status.HTTP_401_UNAUTHORIZED,
                )

        self.assertTrue(ItemFolder.objects.filter(pk=self.node.pk).exists())
        self.assertTrue(
            StudioCharacter.objects.filter(pk=self.character.pk).exists()
        )

    def test_viewer_can_read_but_cannot_mutate_tree(self):
        headers = self._headers(self.viewer_key)
        response = self.client.get(
            "/api/character/select/",
            {"projectId": self.project.id},
            **headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        mutation_calls = {
            "create": lambda: self.client.post(
                "/api/character/create/",
                {
                    "id": "00000000-0000-4000-8000-000000000204",
                    "name": "Viewer leaf",
                    "type": "leaf",
                    "parent": None,
                    "projectId": self.project.id,
                },
                content_type="application/json",
                **headers,
            ),
            "rename": lambda: self.client.post(
                "/api/character/rename/",
                {"id": self.node_id, "name": "Viewer rename"},
                content_type="application/json",
                **headers,
            ),
            "delete": lambda: self.client.post(
                "/api/character/delete/",
                {"id": self.node_id},
                content_type="application/json",
                **headers,
            ),
        }
        for operation, call in mutation_calls.items():
            with self.subTest(operation=operation):
                self.assertEqual(call().status_code, status.HTTP_403_FORBIDDEN)

        self.node.refresh_from_db()
        self.assertEqual(self.node.name, "Mira")
        self.assertTrue(
            StudioCharacter.objects.filter(pk=self.character.pk).exists()
        )

    def test_outsider_cannot_discover_or_mutate_project_tree(self):
        headers = self._headers(self.outsider_key)
        calls = {
            "get": lambda: self.client.get(
                "/api/character/select/",
                {"projectId": self.project.id},
                **headers,
            ),
            "create": lambda: self.client.post(
                "/api/character/create/",
                {
                    "id": "00000000-0000-4000-8000-000000000209",
                    "name": "Outsider leaf",
                    "type": "leaf",
                    "parent": None,
                    "projectId": self.project.id,
                },
                content_type="application/json",
                **headers,
            ),
            "rename": lambda: self.client.post(
                "/api/character/rename/",
                {"id": self.node_id, "name": "Outsider rename"},
                content_type="application/json",
                **headers,
            ),
            "delete": lambda: self.client.post(
                "/api/character/delete/",
                {"id": self.node_id},
                content_type="application/json",
                **headers,
            ),
        }
        for operation, call in calls.items():
            with self.subTest(operation=operation):
                self.assertEqual(call().status_code, status.HTTP_404_NOT_FOUND)

        self.node.refresh_from_db()
        self.assertEqual(self.node.name, "Mira")
        self.assertTrue(
            StudioCharacter.objects.filter(pk=self.character.pk).exists()
        )

    def test_editor_can_create_rename_and_delete_project_tree_nodes(self):
        headers = self._headers(self.editor_key)
        new_character = StudioCharacter.objects.create(
            user=self.owner_key,
            project=self.project,
            name="Editor-created link",
            status=CharacterStatus.ACTIVE,
        )
        new_node_id = "00000000-0000-4000-8000-000000000205"
        create_response = self.client.post(
            "/api/character/create/",
            {
                "id": new_node_id,
                "name": new_character.name,
                "type": "leaf",
                "parent": None,
                "studioCharacterId": str(new_character.character_id),
                "projectId": self.project.id,
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(create_response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            ItemFolder.objects.filter(
                key=new_node_id,
                cur_project=self.project,
            ).exists()
        )

        rename_response = self.client.post(
            "/api/character/rename/",
            {"id": self.node_id, "name": "Renamed by editor"},
            content_type="application/json",
            **headers,
        )
        self.assertEqual(rename_response.status_code, status.HTTP_200_OK)
        self.node.refresh_from_db()
        self.character.refresh_from_db()
        self.assertEqual(self.node.name, "Renamed by editor")
        self.assertEqual(self.character.name, "Renamed by editor")

        delete_response = self.client.post(
            "/api/character/delete/",
            {"id": self.node_id},
            content_type="application/json",
            **headers,
        )
        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)
        self.assertFalse(ItemFolder.objects.filter(pk=self.node.pk).exists())
        self.assertFalse(
            StudioCharacter.objects.filter(pk=self.character.pk).exists()
        )

    def test_create_rejects_cross_project_parent_and_character(self):
        headers = self._headers(self.owner_key)
        parent_response = self.client.post(
            "/api/character/create/",
            {
                "id": "00000000-0000-4000-8000-000000000206",
                "name": "Wrong parent",
                "type": "leaf",
                "parent": str(self.other_parent.key),
                "projectId": self.project.id,
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(parent_response.status_code, status.HTTP_404_NOT_FOUND)

        character_response = self.client.post(
            "/api/character/create/",
            {
                "id": "00000000-0000-4000-8000-000000000207",
                "name": "Wrong character",
                "type": "leaf",
                "parent": None,
                "studioCharacterId": str(self.other_character.character_id),
                "projectId": self.project.id,
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(
            character_response.status_code,
            status.HTTP_404_NOT_FOUND,
        )

        hero_response = self.client.post(
            "/api/character/create/",
            {
                "id": "00000000-0000-4000-8000-000000000210",
                "name": "Wrong legacy character",
                "type": "leaf",
                "parent": None,
                "heroID": self.other_hero.id,
                "projectId": self.project.id,
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(hero_response.status_code, status.HTTP_404_NOT_FOUND)

        duplicate_id_response = self.client.post(
            "/api/character/create/",
            {
                "id": str(self.other_parent.key),
                "name": "Foreign node ID",
                "type": "node",
                "parent": None,
                "projectId": self.project.id,
            },
            content_type="application/json",
            **headers,
        )
        self.assertEqual(
            duplicate_id_response.status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertFalse(
            MenuFolder.objects.filter(
                key=self.other_parent.key,
                cur_project=self.project,
            ).exists()
        )

        self.assertFalse(
            MenuFolder.objects.filter(
                key__in=[
                    "00000000-0000-4000-8000-000000000206",
                    "00000000-0000-4000-8000-000000000207",
                    "00000000-0000-4000-8000-000000000210",
                ]
            ).exists()
        )

    def test_delete_rejects_existing_cross_project_character_link(self):
        malformed = ItemFolder.objects.create(
            key="00000000-0000-4000-8000-000000000208",
            name="Malformed link",
            user=self.owner_key,
            cur_project=self.project,
            is_folder=False,
            studio_character=self.other_character,
        )

        response = self.client.post(
            "/api/character/delete/",
            {"id": str(malformed.key)},
            content_type="application/json",
            **self._headers(self.owner_key),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(ItemFolder.objects.filter(pk=malformed.pk).exists())
        self.assertTrue(
            StudioCharacter.objects.filter(pk=self.other_character.pk).exists()
        )

    def test_delete_rejects_foreign_backlink_to_project_character(self):
        foreign_backlink = ItemFolder.objects.create(
            key="00000000-0000-4000-8000-000000000211",
            name="Foreign backlink",
            user=self.other_owner_key,
            cur_project=self.other_project,
            is_folder=False,
            studio_character=self.character,
        )

        response = self.client.post(
            "/api/character/delete/",
            {"id": self.node_id},
            content_type="application/json",
            **self._headers(self.owner_key),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(ItemFolder.objects.filter(pk=self.node.pk).exists())
        self.assertTrue(
            ItemFolder.objects.filter(pk=foreign_backlink.pk).exists()
        )
        self.assertTrue(
            StudioCharacter.objects.filter(pk=self.character.pk).exists()
        )

    def test_read_rename_and_delete_reject_foreign_legacy_link(self):
        malformed = ItemFolder.objects.create(
            key="00000000-0000-4000-8000-000000000212",
            name="Malformed legacy link",
            user=self.owner_key,
            cur_project=self.project,
            is_folder=False,
            hero=self.other_hero,
        )
        headers = self._headers(self.owner_key)

        read_response = self.client.get(
            "/api/character/select/",
            {"projectId": self.project.id},
            **headers,
        )
        self.assertEqual(read_response.status_code, status.HTTP_200_OK)
        self.assertNotIn(str(malformed.key), read_response.content.decode())

        rename_response = self.client.post(
            "/api/character/rename/",
            {"id": str(malformed.key), "name": "Renamed"},
            content_type="application/json",
            **headers,
        )
        self.assertEqual(
            rename_response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        delete_response = self.client.post(
            "/api/character/delete/",
            {"id": str(malformed.key)},
            content_type="application/json",
            **headers,
        )
        self.assertEqual(
            delete_response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        malformed.refresh_from_db()
        self.assertEqual(malformed.name, "Malformed legacy link")
        self.assertTrue(Character.objects.filter(pk=self.other_hero.pk).exists())

    def test_select_route_does_not_accept_delete_post(self):
        response = self.client.post(
            "/api/character/select/",
            {"id": self.node_id},
            content_type="application/json",
            **self._headers(self.owner_key),
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertTrue(ItemFolder.objects.filter(pk=self.node.pk).exists())
