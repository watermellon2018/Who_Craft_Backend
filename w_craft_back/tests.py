from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import CharacterStatus, StudioCharacter
from w_craft_back.movie.project.models import Project


# Note: the legacy ``MyAPIEndpointTestCase`` (which exercised a `generate_image`
# URL) was removed — that route no longer exists and the test had been failing
# with NoReverseMatch. Image generation is now covered by the character_studio
# test suite via the studio's generation services.


class LoginViewTests(TestCase):
    def test_invalid_credentials_return_unauthorized(self):
        User.objects.create_user(username="owner", password="right-password")

        response = self.client.post(
            reverse("login"),
            {"username": "owner", "password": "wrong-password"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.json(), {"status": "fail"})

    def test_login_creates_missing_user_key(self):
        User.objects.create_user(username="owner", password="password")

        response = self.client.post(
            reverse("login"),
            {"username": "owner", "password": "password"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], status.HTTP_200_OK)
        self.assertTrue(UserKey.objects.filter(user__username="owner").exists())

    def test_login_rejects_get_requests(self):
        User.objects.create_user(username="owner", password="password")

        response = self.client.get(
            reverse("login"),
            {"username": "owner", "password": "password"},
        )

        # GET must not transmit credentials; APIView returns 405.
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


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
        self.client.defaults["HTTP_X_USER_TOKEN"] = str(self.user_key.key)
        self.project = Project.objects.create(
            user=self.user_key,
            title="Tree project",
            format="series",
            annot="Short",
            desc="Long",
        )
        # Use the visible/active status because these tests assert what
        # appears in the tree response. Drafts are intentionally hidden.
        self.character = StudioCharacter.objects.create(
            user=self.user_key,
            project=self.project,
            name="Mira",
            status=CharacterStatus.ACTIVE,
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

    def test_tree_leaf_can_be_created_then_linked(self):
        leaf_id = "00000000-0000-4000-8000-000000000103"

        create_response = self.client.post(
            "/api/character/create/",
            {
                "id": leaf_id,
                "name": "Mira",
                "type": "leaf",
                "parent": None,
                "token_user": str(self.user_key.key),
                "projectId": self.project.id,
            },
            content_type="application/json",
        )
        self.assertEqual(create_response.status_code, status.HTTP_200_OK)

        # A leaf with no studio_character link is a creation-flow artifact
        # (the user opened the create form but hasn't finished). Drafts /
        # dangling leaves are hidden from the tree on purpose — they used
        # to produce duplicate "Энгри дог"-style entries in the sidebar.
        tree_response = self.client.get("/api/character/select/", {"projectId": self.project.id})
        self.assertEqual(tree_response.status_code, status.HTTP_200_OK)
        self.assertEqual(tree_response.json(), [])

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

    def test_tree_hides_leaves_linked_to_draft_characters(self):
        # Even a leaf that IS linked to a studio character should be hidden
        # while the linked character is still a draft — drafts never go in
        # the tree, regardless of how they got there.
        leaf_id = "00000000-0000-4000-8000-000000000105"
        draft = StudioCharacter.objects.create(
            user=self.user_key,
            project=self.project,
            name="Draft hero",
            status=CharacterStatus.DRAFT,
        )

        response = self.client.post(
            "/api/character/create/",
            {
                "id": leaf_id,
                "name": draft.name,
                "type": "leaf",
                "parent": None,
                "studioCharacterId": str(draft.character_id),
                "token_user": str(self.user_key.key),
                "projectId": self.project.id,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        tree_response = self.client.get("/api/character/select/", {"projectId": self.project.id})
        self.assertEqual(tree_response.status_code, status.HTTP_200_OK)
        self.assertEqual(tree_response.json(), [])

    def test_deleting_tree_leaf_removes_linked_studio_character(self):
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

        self.assertFalse(StudioCharacter.objects.filter(character_id=self.character.character_id).exists())
