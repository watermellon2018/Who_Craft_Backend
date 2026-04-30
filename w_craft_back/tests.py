from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from PIL import Image
from rest_framework import status

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import CharacterStatus, StudioCharacter
from w_craft_back.movie.project.models import Project


class MyAPIEndpointTestCase(TestCase):

    @patch("w_craft_back.views.views.create_image_from_string")
    def test_api_request(self, create_image_from_string_mock):
        create_image_from_string_mock.return_value = Image.new("RGB", (1, 1))
        client = Client()

        endpoint_url = reverse('generate_image')

        response = client.get(endpoint_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class LoginViewTests(TestCase):
    def test_invalid_credentials_return_fail_instead_of_error(self):
        User.objects.create_user(username="owner", password="right-password")

        response = self.client.get(
            reverse("login"),
            {"username": "owner", "password": "wrong-password"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"status": "fail"})

    def test_login_creates_missing_user_key(self):
        User.objects.create_user(username="owner", password="password")

        response = self.client.get(
            reverse("login"),
            {"username": "owner", "password": "password"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], status.HTTP_200_OK)
        self.assertTrue(UserKey.objects.filter(user__username="owner").exists())


class ProjectModelTests(TestCase):
    def test_delete_project_without_image_does_not_fail(self):
        user = User.objects.create_user(username="owner", password="password")
        user_key = UserKey.objects.create(user=user)
        project = Project.objects.create(
            user=user_key,
            title="No poster",
            format="series",
            annot="Short",
            desc="Long",
        )

        project.delete()

        self.assertFalse(Project.objects.filter(id=project.id).exists())


class CharacterTreeStudioTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="owner", password="password")
        self.user_key = UserKey.objects.create(user=user)
        self.project = Project.objects.create(
            user=self.user_key,
            title="Tree project",
            format="series",
            annot="Short",
            desc="Long",
        )
        self.character = StudioCharacter.objects.create(
            user=self.user_key,
            project=self.project,
            name="Mira",
        )

    def test_tree_leaf_can_reference_studio_character(self):
        folder_id = "00000000-0000-4000-8000-000000000101"
        leaf_id = "00000000-0000-4000-8000-000000000102"

        folder_response = self.client.post(
            "/api/character/create/",
            {
                "id": folder_id,
                "name": "Scene 1",
                "type": "node",
                "parent": None,
                "token_user": str(self.user_key.key),
                "projectId": self.project.id,
            },
            content_type="application/json",
        )
        self.assertEqual(folder_response.status_code, status.HTTP_200_OK)

        leaf_response = self.client.post(
            "/api/character/create/",
            {
                "id": leaf_id,
                "name": self.character.name,
                "type": "leaf",
                "parent": folder_id,
                "studioCharacterId": str(self.character.character_id),
                "token_user": str(self.user_key.key),
                "projectId": self.project.id,
            },
            content_type="application/json",
        )
        self.assertEqual(leaf_response.status_code, status.HTTP_200_OK)

        tree_response = self.client.get("/api/character/select/", {"projectId": self.project.id})
        self.assertEqual(tree_response.status_code, status.HTTP_200_OK)
        tree = tree_response.json()
        self.assertEqual(tree[0]["name"], "Scene 1")
        self.assertEqual(tree[0]["children"][0]["character_id"], str(self.character.character_id))

    def test_tree_leaf_can_be_created_as_draft_then_linked(self):
        leaf_id = "00000000-0000-4000-8000-000000000103"

        draft_response = self.client.post(
            "/api/character/create/",
            {
                "id": leaf_id,
                "name": "Draft Mira",
                "type": "leaf",
                "parent": None,
                "token_user": str(self.user_key.key),
                "projectId": self.project.id,
            },
            content_type="application/json",
        )
        self.assertEqual(draft_response.status_code, status.HTTP_200_OK)

        draft_tree_response = self.client.get("/api/character/select/", {"projectId": self.project.id})
        self.assertEqual(draft_tree_response.status_code, status.HTTP_200_OK)
        draft_tree = draft_tree_response.json()
        self.assertEqual(draft_tree[0]["name"], "Draft Mira")
        self.assertIsNone(draft_tree[0]["character_id"])

        link_response = self.client.post(
            "/api/character/create/",
            {
                "id": leaf_id,
                "name": self.character.name,
                "type": "leaf",
                "parent": None,
                "studioCharacterId": str(self.character.character_id),
                "token_user": str(self.user_key.key),
                "projectId": self.project.id,
            },
            content_type="application/json",
        )
        self.assertEqual(link_response.status_code, status.HTTP_200_OK)

        linked_tree_response = self.client.get("/api/character/select/", {"projectId": self.project.id})
        self.assertEqual(linked_tree_response.status_code, status.HTTP_200_OK)
        linked_tree = linked_tree_response.json()
        self.assertEqual(linked_tree[0]["name"], self.character.name)
        self.assertEqual(linked_tree[0]["character_id"], str(self.character.character_id))

    def test_deleting_tree_leaf_archives_linked_studio_character(self):
        leaf_id = "00000000-0000-4000-8000-000000000104"

        leaf_response = self.client.post(
            "/api/character/create/",
            {
                "id": leaf_id,
                "name": self.character.name,
                "type": "leaf",
                "parent": None,
                "studioCharacterId": str(self.character.character_id),
                "token_user": str(self.user_key.key),
                "projectId": self.project.id,
            },
            content_type="application/json",
        )
        self.assertEqual(leaf_response.status_code, status.HTTP_200_OK)

        delete_response = self.client.post(
            "/api/character/delete/",
            {"id": leaf_id},
            content_type="application/json",
        )
        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)

        self.character.refresh_from_db()
        self.assertEqual(self.character.status, CharacterStatus.ARCHIVED)
        self.assertIsNotNone(self.character.archived_at)
