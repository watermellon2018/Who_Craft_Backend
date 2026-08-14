from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectAsset,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project

from w_craft_back.upload_protection import (
    UploadLimitExceeded,
    UploadProtectionMiddleware,
)


SMALL_ENDPOINT_LIMITS = {
    "character-create-from-reference": 32,
    "character-outfit-reference-upload": 32,
    "character-clothing-reference-upload": 32,
    "character-reference-upload": 32,
    "project-assets": 32,
    "profile-me-avatar": 32,
    "profile-me-cover": 32,
}


@override_settings(
    UPLOAD_ENDPOINT_FILE_LIMITS=SMALL_ENDPOINT_LIMITS,
    UPLOAD_MULTIPART_OVERHEAD_BYTES=32,
)
class UploadEndpointBodyCapTests(SimpleTestCase):
    def test_declared_oversized_multipart_is_rejected_for_every_upload_route(
        self,
    ):
        character_id = UUID(int=1)
        outfit_id = UUID(int=2)
        paths = (
            "/api/projects/1/characters/from-reference",
            (
                f"/api/projects/1/characters/{character_id}"
                f"/outfits/{outfit_id}/upload-reference"
            ),
            f"/api/projects/1/characters/{character_id}/clothing-references",
            f"/api/projects/1/characters/{character_id}/references/upload",
            "/api/projects/1/assets/",
            "/api/profile/me/avatar/",
            "/api/profile/me/cover/",
        )

        for path in paths:
            with self.subTest(path=path):
                response = Client().post(
                    path,
                    {
                        "file": SimpleUploadedFile(
                            "oversized.bin",
                            b"x" * 256,
                            content_type="application/octet-stream",
                        )
                    },
                )

                self.assertEqual(response.status_code, 413, response.content)
                self.assertEqual(
                    response.json()["error_code"],
                    "UPLOAD_TOO_LARGE",
                )

    def test_declared_oversized_body_is_rejected_without_parsing(self):
        request = RequestFactory().post(
            "/api/profile/me/avatar/",
            {
                "file": SimpleUploadedFile(
                    "oversized.bin",
                    b"x" * 256,
                    content_type="application/octet-stream",
                )
            },
        )
        request.resolver_match = SimpleNamespace(url_name="profile-me-avatar")
        middleware = UploadProtectionMiddleware(lambda _: HttpResponse())

        response = middleware.process_view(request, None, (), {})

        self.assertEqual(response.status_code, 413)
        self.assertFalse(request._read_started)
        self.assertFalse(hasattr(request, "_files"))

    @override_settings(
        UPLOAD_ENDPOINT_FILE_LIMITS={},
        UPLOAD_DEFAULT_MULTIPART_FILE_LIMIT_BYTES=32,
        UPLOAD_MULTIPART_OVERHEAD_BYTES=32,
    )
    def test_unlisted_route_uses_default_body_cap(self):
        request = RequestFactory().post(
            "/api/profile/me/",
            {"notes": "x" * 256},
            content_type="application/json",
        )
        request.resolver_match = SimpleNamespace(url_name="profile-me")
        middleware = UploadProtectionMiddleware(lambda _: HttpResponse())

        response = middleware.process_view(request, None, (), {})

        self.assertEqual(response.status_code, 413)
        self.assertFalse(request._read_started)
        self.assertFalse(hasattr(request, "_files"))

    def test_lengthless_chunked_multipart_is_rejected_before_parsing(self):
        request = RequestFactory().post(
            "/api/profile/me/avatar/",
            {
                "file": SimpleUploadedFile(
                    "chunked.bin",
                    b"x" * 256,
                    content_type="application/octet-stream",
                )
            },
        )
        request.META.pop("CONTENT_LENGTH")
        request.META["HTTP_TRANSFER_ENCODING"] = "chunked"
        request.resolver_match = SimpleNamespace(url_name="profile-me-avatar")
        middleware = UploadProtectionMiddleware(lambda _: HttpResponse())

        response = middleware.process_view(request, None, (), {})

        self.assertEqual(response.status_code, 413)
        self.assertFalse(request._read_started)
        self.assertFalse(hasattr(request, "_files"))


class StreamingMultipartCleanupTests(SimpleTestCase):
    def test_understated_stream_stops_early_and_removes_temp_file(self):
        max_file_bytes = 64 * 1024
        with tempfile.TemporaryDirectory() as upload_dir:
            with override_settings(
                FILE_UPLOAD_TEMP_DIR=upload_dir,
                UPLOAD_ENDPOINT_FILE_LIMITS={
                    "profile-me-avatar": max_file_bytes,
                },
                UPLOAD_MULTIPART_OVERHEAD_BYTES=64 * 1024,
            ):
                request = RequestFactory().post(
                    "/api/profile/me/avatar/",
                    {
                        "file": SimpleUploadedFile(
                            "streamed.bin",
                            b"x" * (512 * 1024),
                            content_type="application/octet-stream",
                        )
                    },
                )
                full_body_size = int(request.META["CONTENT_LENGTH"])
                request.META["CONTENT_LENGTH"] = "1"
                request.resolver_match = SimpleNamespace(
                    url_name="profile-me-avatar"
                )
                middleware = UploadProtectionMiddleware(
                    lambda _: HttpResponse(status=200)
                )

                self.assertIsNone(
                    middleware.process_view(request, None, (), {})
                )
                with self.assertRaises(UploadLimitExceeded) as caught:
                    request.FILES
                response = middleware.process_exception(
                    request,
                    caught.exception,
                )

                self.assertEqual(response.status_code, 413)
                self.assertLess(request._stream.bytes_read, full_body_size)
                self.assertEqual(list(Path(upload_dir).iterdir()), [])

    def test_completed_file_is_removed_and_view_does_not_mutate(self):
        max_file_bytes = 32 * 1024
        with tempfile.TemporaryDirectory() as upload_dir:
            with override_settings(
                FILE_UPLOAD_TEMP_DIR=upload_dir,
                UPLOAD_ENDPOINT_FILE_LIMITS={
                    "profile-me-avatar": max_file_bytes,
                },
                UPLOAD_MULTIPART_OVERHEAD_BYTES=512 * 1024,
            ):
                request = RequestFactory().post(
                    "/api/profile/me/avatar/",
                    {
                        "file": [
                            SimpleUploadedFile("first.bin", b"a" * 1024),
                            SimpleUploadedFile(
                                "second.bin",
                                b"b" * (128 * 1024),
                            ),
                        ]
                    },
                )
                request.resolver_match = SimpleNamespace(
                    url_name="profile-me-avatar"
                )
                mutations: list[int] = []

                def upload_view(current_request):
                    files = current_request.FILES.getlist("file")
                    mutations.append(len(files))
                    return HttpResponse(status=201)

                middleware = UploadProtectionMiddleware(upload_view)
                self.assertIsNone(
                    middleware.process_view(request, None, (), {})
                )

                with self.assertRaises(UploadLimitExceeded) as caught:
                    middleware(request)
                response = middleware.process_exception(
                    request,
                    caught.exception,
                )

                self.assertEqual(response.status_code, 413)
                self.assertEqual(mutations, [])
                self.assertEqual(list(Path(upload_dir).iterdir()), [])

    def test_stream_limit_counts_large_non_file_multipart_fields(self):
        max_file_bytes = 64 * 1024
        body_limit = max_file_bytes + (64 * 1024)
        with override_settings(
            UPLOAD_ENDPOINT_FILE_LIMITS={
                "profile-me-avatar": max_file_bytes,
            },
            UPLOAD_MULTIPART_OVERHEAD_BYTES=64 * 1024,
        ):
            request = RequestFactory().post(
                "/api/profile/me/avatar/",
                {"notes": "x" * (512 * 1024)},
            )
            full_body_size = int(request.META["CONTENT_LENGTH"])
            request.META["CONTENT_LENGTH"] = "1"
            request.resolver_match = SimpleNamespace(
                url_name="profile-me-avatar"
            )
            middleware = UploadProtectionMiddleware(lambda _: HttpResponse())

            self.assertIsNone(
                middleware.process_view(request, None, (), {})
            )
            with self.assertRaises(UploadLimitExceeded):
                request.POST

            self.assertLessEqual(request._stream.bytes_read, body_limit)
            self.assertLess(request._stream.bytes_read, full_body_size)


class UploadEndpointCompatibilityTests(TestCase):
    def test_small_project_asset_still_uploads(self):
        user = User.objects.create_user(username="upload-owner")
        user_key = UserKey.objects.create(user=user)
        project = Project.objects.create(
            owner=user,
            title="Upload project",
            format="full-movie",
        )
        ProjectMember.objects.create(
            project=project,
            user=user,
            role=ProjectMemberRole.OWNER,
        )
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
            "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                response = APIClient().post(
                    f"/api/projects/{project.id}/assets/",
                    {
                        "file": SimpleUploadedFile(
                            "reference.png",
                            png,
                            content_type="image/png",
                        ),
                        "asset_type": "reference",
                        "title": "Reference",
                    },
                    format="multipart",
                    HTTP_X_USER_TOKEN=user_key.key,
                )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["mime_type"], "image/png")

    def test_stream_rejection_has_headers_and_creates_no_asset(self):
        user = User.objects.create_user(username="stream-owner")
        user_key = UserKey.objects.create(user=user)
        project = Project.objects.create(
            owner=user,
            title="Stream project",
            format="full-movie",
        )
        ProjectMember.objects.create(
            project=project,
            user=user,
            role=ProjectMemberRole.OWNER,
        )

        with tempfile.TemporaryDirectory() as upload_dir:
            with override_settings(
                FILE_UPLOAD_TEMP_DIR=upload_dir,
                UPLOAD_ENDPOINT_FILE_LIMITS={"project-assets": 32},
                UPLOAD_MULTIPART_OVERHEAD_BYTES=1024 * 1024,
                CORS_ALLOWED_ORIGINS=["http://localhost:3000"],
            ):
                response = APIClient().post(
                    f"/api/projects/{project.id}/assets/",
                    {
                        "file": SimpleUploadedFile(
                            "streamed.bin",
                            b"x" * 256,
                            content_type="application/octet-stream",
                        ),
                        "asset_type": "reference",
                    },
                    format="multipart",
                    HTTP_X_USER_TOKEN=user_key.key,
                    HTTP_ORIGIN="http://localhost:3000",
                )
                remaining_temp_files = list(Path(upload_dir).iterdir())

        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response["Access-Control-Allow-Origin"],
            "http://localhost:3000",
        )
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(remaining_temp_files, [])
        self.assertFalse(ProjectAsset.objects.filter(project=project).exists())

    def test_character_stream_rejection_is_413_and_creates_nothing(self):
        user = User.objects.create_user(username="character-stream-owner")
        user_key = UserKey.objects.create(user=user)
        project = Project.objects.create(
            owner=user,
            title="Character stream project",
            format="full-movie",
        )
        ProjectMember.objects.create(
            project=project,
            user=user,
            role=ProjectMemberRole.OWNER,
        )

        with tempfile.TemporaryDirectory() as upload_dir:
            with override_settings(
                FILE_UPLOAD_TEMP_DIR=upload_dir,
                UPLOAD_ENDPOINT_FILE_LIMITS={
                    "character-create-from-reference": 32,
                },
                UPLOAD_MULTIPART_OVERHEAD_BYTES=1024 * 1024,
            ):
                response = APIClient().post(
                    f"/api/projects/{project.id}/characters/from-reference",
                    {
                        "reference_image": SimpleUploadedFile(
                            "streamed.png",
                            b"x" * 256,
                            content_type="image/png",
                        ),
                        "name": "Rejected character",
                    },
                    format="multipart",
                    HTTP_X_USER_TOKEN=user_key.key,
                )
                remaining_temp_files = list(Path(upload_dir).iterdir())

        self.assertEqual(response.status_code, 413, response.content)
        self.assertEqual(response.json()["error_code"], "UPLOAD_TOO_LARGE")
        self.assertEqual(remaining_temp_files, [])
        self.assertFalse(StudioCharacter.objects.filter(project=project).exists())
