"""Security regressions for the P1-5.5 upload and media boundary."""

from __future__ import annotations

import io
import os
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image, PngImagePlugin
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetType,
    CharacterImage,
    CharacterImageType,
    StudioCharacter,
)
from w_craft_back.movie.poster.file_validation import (
    ReferenceImageValidationError,
    validate_reference_image,
)
from w_craft_back.movie.project.dashboard_models import (
    ProjectAsset,
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.profile.models import UserAsset, UserProfile
from w_craft_back.profile.services import delete_image
from w_craft_back.storage_gateway import (
    InvalidImage,
    UnsafeRemoteMedia,
    _is_public_ip,
    _resolve_remote_target,
    fetch_remote_image,
    normalize_image_bytes,
    signed_media_url,
    signed_url_for_asset,
    store_image_upload,
)


def _png_bytes(
    width: int = 4,
    height: int = 4,
    *,
    comment: str | None = None,
) -> bytes:
    buffer = io.BytesIO()
    png_info = None
    if comment is not None:
        png_info = PngImagePlugin.PngInfo()
        png_info.add_text("Comment", comment)
    Image.new("RGB", (width, height), color=(30, 90, 140)).save(
        buffer,
        format="PNG",
        pnginfo=png_info,
    )
    return buffer.getvalue()


class ImageNormalizationTests(SimpleTestCase):
    def test_decode_reencode_strips_metadata_and_enforces_pixels(self):
        normalized = normalize_image_bytes(
            _png_bytes(comment="untrusted metadata"),
        )
        self.assertEqual(normalized.mime_type, "image/png")
        with Image.open(io.BytesIO(normalized.data)) as image:
            self.assertNotIn("Comment", image.info)

        with self.assertRaises(InvalidImage):
            normalize_image_bytes(_png_bytes(3, 3), max_pixels=4)

    def test_fake_image_is_rejected_regardless_of_declared_mime(self):
        upload = SimpleUploadedFile(
            "../../portrait.png",
            b"<script>alert(1)</script>",
            content_type="image/png",
        )
        with self.assertRaises(ReferenceImageValidationError):
            validate_reference_image(upload)

    @override_settings(MEDIA_ROOT=tempfile.gettempdir())
    def test_storage_name_is_generated(self):
        upload = SimpleUploadedFile(
            "../../attacker name.png",
            _png_bytes(),
            content_type="application/octet-stream",
        )
        stored = store_image_upload(upload, namespace="profiles/42/avatar")
        self.addCleanup(default_storage.delete, stored.storage_key)
        self.assertTrue(stored.storage_key.startswith("profiles/42/avatar/"))
        self.assertNotIn("attacker", stored.storage_key)
        self.assertNotIn("..", stored.storage_key)

    def test_exif_rotation_metadata_matches_reencoded_dimensions(self):
        source = io.BytesIO()
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (2, 3), color=(30, 90, 140)).save(
            source,
            format="JPEG",
            exif=exif,
        )

        normalized = normalize_image_bytes(source.getvalue())

        self.assertEqual((normalized.width, normalized.height), (3, 2))
        with Image.open(io.BytesIO(normalized.data)) as stored:
            self.assertEqual(stored.size, (3, 2))


class SignedUrlCompatibilityTests(SimpleTestCase):
    @override_settings(SIGNED_MEDIA_BASE_URL="", PUBLIC_BASE_URL="http://api.test:8000")
    def test_urls_are_absolute_without_request_and_external_legacy_is_preserved(self):
        self.assertTrue(
            signed_media_url("character-studio/x.png").startswith(
                "http://api.test:8000/api/media/"
            )
        )
        self.assertEqual(
            signed_url_for_asset(
                storage_key=None,
                legacy_url="https://cdn.example/x.png",
            ),
            "https://cdn.example/x.png",
        )
        self.assertIsNone(
            signed_url_for_asset(storage_key=None, legacy_url="javascript:alert(1)")
        )

    @override_settings(
        SIGNED_MEDIA_BASE_URL="",
        PUBLIC_BASE_URL="http://stale.invalid",
        ALLOWED_HOSTS=["api.correct"],
    )
    def test_request_origin_takes_precedence_over_public_fallback(self):
        request = RequestFactory().get("/", HTTP_HOST="api.correct")

        result = signed_media_url("character-studio/x.png", request=request)

        self.assertTrue(result.startswith("http://api.correct/api/media/"))


class RemoteFetchBoundaryTests(SimpleTestCase):
    @patch("w_craft_back.storage_gateway.socket.getaddrinfo")
    def test_private_or_mixed_dns_answers_are_rejected(self, getaddrinfo):
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]
        with self.assertRaises(UnsafeRemoteMedia):
            _resolve_remote_target("https://provider.example/image.png")

    def test_cgnat_is_not_a_public_fetch_target(self):
        self.assertFalse(_is_public_ip("100.64.0.1"))

    @patch("urllib3.HTTPSConnectionPool")
    @patch("w_craft_back.storage_gateway._resolve_remote_target")
    def test_fetch_connects_to_pinned_ip_with_original_tls_identity(
        self,
        resolve_target,
        pool_class,
    ):
        resolve_target.return_value = (
            urlparse("https://provider.example/output.png?x=1"),
            "93.184.216.34",
            443,
        )
        response = SimpleNamespace(
            status=200,
            headers={
                "Content-Type": "image/png",
                "Content-Length": str(len(_png_bytes())),
            },
            stream=lambda _size: iter([_png_bytes()]),
            release_conn=MagicMock(),
        )
        pool = pool_class.return_value
        pool.urlopen.return_value = response

        normalized = fetch_remote_image(
            "https://provider.example/output.png?x=1",
        )

        self.assertEqual(normalized.mime_type, "image/png")
        args, kwargs = pool_class.call_args
        self.assertEqual(args[0], "93.184.216.34")
        self.assertEqual(kwargs["server_hostname"], "provider.example")
        self.assertEqual(kwargs["assert_hostname"], "provider.example")
        request_kwargs = pool.urlopen.call_args.kwargs
        self.assertEqual(
            pool.urlopen.call_args.args[:2],
            ("GET", "/output.png?x=1"),
        )
        self.assertEqual(
            request_kwargs["headers"]["Host"],
            "provider.example",
        )


@override_settings(SIGNED_MEDIA_TTL_SECONDS=300)
class StorageBoundaryIntegrationTests(TestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.media_override = override_settings(
            MEDIA_ROOT=self.media_directory.name,
        )
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media_directory.cleanup)

        self.client = APIClient()
        self.owner = User.objects.create_user(username="media-owner")
        self.owner_key = UserKey.objects.create(user=self.owner)
        self.owner_token = str(self.owner_key.key)
        self.viewer = User.objects.create_user(username="media-viewer")
        self.viewer_key = UserKey.objects.create(user=self.viewer)
        self.viewer_token = str(self.viewer_key.key)
        self.outsider = User.objects.create_user(username="media-outsider")
        self.outsider_key = UserKey.objects.create(user=self.outsider)
        self.outsider_token = str(self.outsider_key.key)
        self.project = Project.objects.create(
            owner=self.owner,
            user=self.owner_key,
            title="Media boundary",
            format="series",
            annot="",
            desc="",
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.owner,
            role=ProjectMemberRole.OWNER,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.viewer,
            role=ProjectMemberRole.VIEWER,
        )

    def _headers(self, token: str) -> dict[str, str]:
        return {"HTTP_X_USER_TOKEN": token}

    def test_project_asset_is_canonicalized_signed_and_acl_scoped(self):
        upload_url = f"/api/projects/{self.project.id}/assets/"
        response = self.client.post(
            upload_url,
            {
                "asset_type": "reference",
                "title": "Portrait",
                "file": SimpleUploadedFile(
                    "../../portrait.png",
                    _png_bytes(),
                    content_type="text/html",
                ),
            },
            format="multipart",
            **self._headers(self.owner_token),
        )
        self.assertEqual(response.status_code, 201, response.content)
        asset = ProjectAsset.objects.get(pk=response.json()["id"])
        self.assertTrue(
            asset.file.name.startswith(
                f"projects/{self.project.id}/assets/reference/"
            )
        )
        self.assertNotIn("portrait", asset.file.name)
        self.assertEqual(asset.metadata["mime_type"], "image/png")
        self.assertEqual(asset.metadata["width"], 4)
        self.assertTrue(urlparse(response.json()["url"]).path.startswith("/api/media/"))

        detail_url = (
            f"/api/projects/{self.project.id}/assets/{asset.id}/"
        )
        viewer_response = self.client.get(
            detail_url,
            **self._headers(self.viewer_token),
        )
        self.assertEqual(viewer_response.status_code, 200)
        signed_url = viewer_response.json()["url"]
        self.assertEqual(self.client.get(signed_url).status_code, 200)
        self.assertEqual(
            self.client.get(f"/media/{asset.file.name}").status_code,
            404,
        )

        outsider_response = self.client.get(
            detail_url,
            **self._headers(self.outsider_token),
        )
        self.assertEqual(outsider_response.status_code, 403)

    def test_invalid_project_and_profile_images_are_not_persisted(self):
        project_response = self.client.post(
            f"/api/projects/{self.project.id}/assets/",
            {
                "asset_type": "image",
                "file": SimpleUploadedFile(
                    "fake.png",
                    b"not an image",
                    content_type="image/png",
                ),
            },
            format="multipart",
            **self._headers(self.owner_token),
        )
        self.assertEqual(project_response.status_code, 400)
        self.assertFalse(ProjectAsset.objects.exists())

        profile_response = self.client.post(
            "/api/profile/me/avatar/",
            {
                "file": SimpleUploadedFile(
                    "fake.png",
                    b"not an image",
                    content_type="image/png",
                )
            },
            format="multipart",
            **self._headers(self.owner_token),
        )
        self.assertEqual(profile_response.status_code, 415)

    def test_shared_character_binary_survives_until_last_reference(self):
        key = default_storage.save(
            "character-studio/shared/reference.png",
            ContentFile(_png_bytes()),
        )
        character = StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Mira",
        )
        asset = CharacterAsset.objects.create(
            character=character,
            project=self.project,
            user=self.owner_key,
            asset_type=CharacterAssetType.PORTRAIT,
            storage_path=key,
            mime_type="image/png",
        )
        image = CharacterImage.objects.create(
            character=character,
            asset=asset,
            image_type=CharacterImageType.PORTRAIT,
            storage_path=key,
            image_url=f"/media/{key}",
        )

        with self.captureOnCommitCallbacks(execute=True):
            asset.delete()
        self.assertTrue(default_storage.exists(key))

        with self.captureOnCommitCallbacks(execute=True):
            image.delete()
        self.assertFalse(default_storage.exists(key))

    def test_signed_delivery_supports_single_byte_ranges(self):
        key = default_storage.save(
            "projects/range/audio/sample.mp3",
            ContentFile(b"0123456789"),
        )
        self.addCleanup(default_storage.delete, key)
        signed_url = signed_media_url(key)

        response = self.client.get(
            urlparse(signed_url).path,
            HTTP_RANGE="bytes=2-4",
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 2-4/10")
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(b"".join(response.streaming_content), b"234")

    def test_recent_soft_deleted_profile_asset_is_retained(self):
        key = default_storage.save(
            "profiles/retention/avatar/image.png",
            ContentFile(_png_bytes()),
        )
        asset = UserAsset.objects.create(
            user=self.owner,
            type=UserAsset.AVATAR,
            storage_key=key,
            mime_type="image/png",
        )
        profile, _ = UserProfile.objects.get_or_create(user=self.owner)
        profile.avatar = key
        profile.avatar_asset = asset
        profile.save(update_fields=["avatar", "avatar_asset"])

        with self.captureOnCommitCallbacks(execute=True):
            delete_image(self.owner, UserAsset.AVATAR)

        asset.refresh_from_db()
        self.assertIsNotNone(asset.deleted_at)
        self.assertTrue(default_storage.exists(key))

        UserAsset.objects.filter(pk=asset.pk).update(
            deleted_at=timezone.now() - timedelta(hours=2),
        )
        path = default_storage.path(key)
        old_timestamp = max(1, os.path.getmtime(path) - 8 * 60 * 60)
        os.utime(path, (old_timestamp, old_timestamp))
        call_command(
            "sweep_orphan_media",
            retention_hours=1,
            limit=10,
            delete=True,
            verbosity=0,
        )
        self.assertFalse(default_storage.exists(key))

    def test_orphan_sweeper_is_dry_run_by_default(self):
        key = default_storage.save(
            "projects/orphans/stale.png",
            ContentFile(_png_bytes()),
        )
        path = default_storage.path(key)
        old_timestamp = max(1, os.path.getmtime(path) - 8 * 60 * 60)
        os.utime(path, (old_timestamp, old_timestamp))

        call_command(
            "sweep_orphan_media",
            retention_hours=1,
            limit=10,
            verbosity=0,
        )
        self.assertTrue(default_storage.exists(key))

        call_command(
            "sweep_orphan_media",
            retention_hours=1,
            limit=10,
            delete=True,
            verbosity=0,
        )
        self.assertFalse(default_storage.exists(key))
