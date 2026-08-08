from __future__ import annotations

import io
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
    Scene,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.reference_library.models import (
    ProjectReference,
    ReferenceGenerationJob,
    ReferenceJobStatus,
    SceneReference,
)
from w_craft_back.movie.reference_library.worker import execute_reference_job


def make_user(username: str) -> tuple[User, str]:
    user = User.objects.create_user(username=username, password="pw")
    key = UserKey.objects.create(user=user)
    return user, str(key.key)


def make_project(owner: User) -> Project:
    project = Project.objects.create(
        owner=owner,
        user=UserKey.objects.get(user=owner),
        title="Reference film",
        format="full-movie",
        annot="",
        desc="",
    )
    ProjectMember.objects.create(
        project=project,
        user=owner,
        role=ProjectMemberRole.OWNER,
    )
    return project


def png_upload(name: str = "reference.png") -> SimpleUploadedFile:
    output = io.BytesIO()
    Image.new("RGB", (80, 60), (170, 20, 30)).save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


@override_settings(REFERENCE_IMAGE_PROVIDER="mock", SIGNED_MEDIA_TTL_SECONDS=120)
class ReferenceApiTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.client = APIClient()
        self.owner, self.owner_token = make_user("reference-owner")
        self.viewer, self.viewer_token = make_user("reference-viewer")
        self.outsider, self.outsider_token = make_user("reference-outsider")
        self.project = make_project(self.owner)
        ProjectMember.objects.create(
            project=self.project,
            user=self.viewer,
            role=ProjectMemberRole.VIEWER,
        )
        self.scene = Scene.objects.create(
            project=self.project,
            title="Scene 1",
            order=1,
            created_by=self.owner,
            updated_by=self.owner,
        )

    @property
    def collection_url(self) -> str:
        return f"/api/projects/{self.project.id}/references/"

    def create_reference(self) -> dict:
        response = self.client.post(
            self.collection_url,
            {
                "title": "Red medallion",
                "category": "prop",
                "description": "An old silver medallion with red enamel",
                "brief": {
                    "schemaVersion": "reference_brief.v1",
                    "aspectRatio": "1:1",
                    "materials": ["silver", "red enamel"],
                },
                "tags": ["hero", "Hero"],
            },
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_viewer_can_list_but_cannot_create(self):
        listed = self.client.get(
            self.collection_url,
            HTTP_X_USER_TOKEN=self.viewer_token,
        )
        denied = self.client.post(
            self.collection_url,
            {"title": "Forbidden", "category": "prop"},
            format="json",
            HTTP_X_USER_TOKEN=self.viewer_token,
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["code"], "REFERENCE_EDIT_FORBIDDEN")

    def test_mock_generation_apply_and_scene_pin_core_flow(self):
        created = self.create_reference()
        reference_id = created["id"]
        jobs_url = f"{self.collection_url}{reference_id}/generation-jobs/"
        enqueued = self.client.post(
            jobs_url,
            {
                "expectedReferenceVersion": 1,
                "operation": "generate",
                "variantCount": 2,
                "brief": {
                    "schemaVersion": "reference_brief.v1",
                    "aspectRatio": "1:1",
                    "description": "An old red silver medallion",
                },
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="reference-core-flow",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(enqueued.status_code, 202, enqueued.content)
        job_id = enqueued.json()["id"]

        execute_reference_job(job_id)
        job = ReferenceGenerationJob.objects.get(pk=job_id)
        self.assertEqual(job.status, ReferenceJobStatus.COMPLETED)
        self.assertEqual(job.requested_model, "reference-mock-v1")
        self.assertEqual(job.variants.count(), 2)
        variant = job.variants.order_by("variant_index").first()

        applied = self.client.post(
            f"{jobs_url}{job_id}/variants/{variant.id}/apply/",
            {"expectedReferenceVersion": 1},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(applied.status_code, 201, applied.content)
        version_id = applied.json()["activeVersion"]["id"]

        replayed = self.client.post(
            f"{jobs_url}{job_id}/variants/{variant.id}/apply/",
            {"expectedReferenceVersion": 1},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(replayed.status_code, 200, replayed.content)
        self.assertEqual(replayed.json()["activeVersion"]["id"], version_id)

        assigned = self.client.put(
            f"/api/projects/{self.project.id}/scenes/{self.scene.id}/references/",
            {
                "expectedSceneVersion": 1,
                "items": [
                    {
                        "referenceId": reference_id,
                        "versionId": version_id,
                        "usage": "hero_prop",
                        "note": "In Anna's hand",
                    }
                ],
            },
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(assigned.status_code, 200, assigned.content)
        self.assertEqual(assigned.json()["sceneVersion"], 2)
        self.assertEqual(SceneReference.objects.count(), 1)

    def test_cancelled_job_retry_capability_respects_archive_state(self):
        created = self.create_reference()
        jobs_url = f"{self.collection_url}{created['id']}/generation-jobs/"
        enqueued = self.client.post(
            jobs_url,
            {
                "expectedReferenceVersion": 1,
                "operation": "generate",
                "variantCount": 1,
                "brief": {
                    "schemaVersion": "reference_brief.v1",
                    "description": "A clear prop reference",
                },
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-retry-capability",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        job_id = enqueued.json()["id"]
        cancelled = self.client.post(
            f"{jobs_url}{job_id}/cancellation-request/",
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertTrue(cancelled.json()["canRetry"])

        archived = self.client.post(
            f"{self.collection_url}{created['id']}/archive/",
            {"expectedReferenceVersion": 1},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(archived.status_code, 200, archived.content)
        detail = self.client.get(
            f"{jobs_url}{job_id}/",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertFalse(detail.json()["canRetry"])

    def test_upload_requires_rights_and_creates_active_immutable_version(self):
        created = self.create_reference()
        upload_url = f"{self.collection_url}{created['id']}/versions/upload/"
        denied = self.client.post(
            upload_url,
            {
                "file": png_upload(),
                "expectedReferenceVersion": 1,
                "rightsConfirmed": False,
                "rightsStatementVersion": "reference-upload-v1",
            },
            format="multipart",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(denied.json()["code"], "REFERENCE_UPLOAD_RIGHTS_REQUIRED")

        uploaded = self.client.post(
            upload_url,
            {
                "file": png_upload(),
                "expectedReferenceVersion": 1,
                "rightsConfirmed": True,
                "rightsStatementVersion": "reference-upload-v1",
            },
            format="multipart",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.content)
        reference = ProjectReference.objects.get(pk=created["id"])
        self.assertEqual(reference.active_version.version_number, 1)
        self.assertEqual(reference.active_version.asset.asset_type, "reference")
        self.assertEqual(reference.active_version.thumbnail_asset.asset_type, "image")

    def test_project_with_pinned_reference_can_be_deleted(self):
        created = self.create_reference()
        upload_url = f"{self.collection_url}{created['id']}/versions/upload/"
        uploaded = self.client.post(
            upload_url,
            {
                "file": png_upload(),
                "expectedReferenceVersion": 1,
                "rightsConfirmed": True,
                "rightsStatementVersion": "reference-upload-v1",
            },
            format="multipart",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        version_id = uploaded.json()["activeVersion"]["id"]
        assigned = self.client.put(
            f"/api/projects/{self.project.id}/scenes/{self.scene.id}/references/",
            {
                "expectedSceneVersion": 1,
                "items": [
                    {
                        "referenceId": created["id"],
                        "versionId": version_id,
                        "usage": "hero_prop",
                        "note": "Pinned before project deletion",
                    }
                ],
            },
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(assigned.status_code, 200, assigned.content)

        self.project.delete()

        self.assertFalse(Project.objects.filter(pk=self.project.id).exists())
        self.assertFalse(ProjectReference.objects.filter(pk=created["id"]).exists())

    def test_idempotency_replay_and_mismatch(self):
        created = self.create_reference()
        jobs_url = f"{self.collection_url}{created['id']}/generation-jobs/"
        payload = {
            "expectedReferenceVersion": 1,
            "operation": "generate",
            "variantCount": 1,
            "brief": {
                "schemaVersion": "reference_brief.v1",
                "description": "A clear prop reference",
            },
        }
        first = self.client.post(
            jobs_url,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="same-key",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        replay = self.client.post(
            jobs_url,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="same-key",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        changed = {**payload, "variantCount": 2}
        mismatch = self.client.post(
            jobs_url,
            changed,
            format="json",
            HTTP_IDEMPOTENCY_KEY="same-key",
            HTTP_X_USER_TOKEN=self.owner_token,
        )

        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(replay.status_code, 202, replay.content)
        self.assertEqual(first.json()["id"], replay.json()["id"])
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.json()["code"], "REFERENCE_IDEMPOTENCY_MISMATCH")
