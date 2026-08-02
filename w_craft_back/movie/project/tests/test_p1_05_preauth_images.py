import base64
import io

from django.contrib.auth.models import User
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.project_images import (
    decode_project_image_data_url,
)
from w_craft_back.movie.properties.models import Genre


_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
    "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ProjectMutationPreauthorizationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        owner = User.objects.create_user(username="preauth-owner")
        owner_key = UserKey.objects.create(user=owner)
        self.viewer = User.objects.create_user(username="preauth-viewer")
        viewer_key = UserKey.objects.create(user=self.viewer)
        self.viewer_token = viewer_key.key
        self.project = Project.objects.create(
            owner=owner,
            user=owner_key,
            title="Preauth",
            format="full-movie",
            annot="",
            desc="",
        )
        ProjectMember.objects.create(
            project=self.project,
            user=owner,
            role=ProjectMemberRole.OWNER,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.viewer,
            role=ProjectMemberRole.VIEWER,
        )

    def test_viewer_cannot_create_global_genre_before_settings_check(self):
        genre_name = "Viewer injected genre"

        response = self.client.patch(
            f"/api/projects/{self.project.id}/",
            {"genre": [genre_name]},
            format="json",
            HTTP_X_USER_TOKEN=self.viewer_token,
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Genre.objects.filter(name=genre_name).exists())

    def test_canonical_update_authorizes_before_image_validation(self):
        response = self.client.patch(
            f"/api/projects/{self.project.id}/",
            {"poster_image_data": "data:text/plain;base64,not-an-image"},
            format="json",
            HTTP_X_USER_TOKEN=self.viewer_token,
        )

        self.assertEqual(response.status_code, 403, response.content)


class ProjectImagePayloadTests(TestCase):
    def test_accepts_verified_allowed_image(self):
        image = decode_project_image_data_url(
            f"data:image/png;base64,{_PNG_1X1}",
            owner_id=1,
            title="Poster",
        )

        self.assertIsNotNone(image)
        with Image.open(io.BytesIO(image.read())) as decoded:
            self.assertEqual(decoded.format, "PNG")
            self.assertEqual(decoded.size, (1, 1))

    def test_rejects_spoofed_or_oversized_payload(self):
        spoofed = decode_project_image_data_url(
            "data:image/png;base64,"
            + base64.b64encode(b"not an image").decode("ascii"),
            owner_id=1,
            title="Poster",
        )
        oversized = decode_project_image_data_url(
            "data:image/png;base64," + ("A" * (7 * 1024 * 1024)),
            owner_id=1,
            title="Poster",
        )

        self.assertIsNone(spoofed)
        self.assertIsNone(oversized)
