from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from w_craft_back.auth.models import UserKey
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
        self.assertEqual(response.json()["status"], "fail")

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
        UserKey.objects.create(user=user)
        project = Project.objects.create(
            owner=user,
            title="No poster",
            format="series",
            annotation="Short",
            synopsis="Long",
        )

        project.delete()

        self.assertFalse(Project.objects.filter(id=project.id).exists())
