from __future__ import annotations

import io
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.credits.models import CreditAccount
from w_craft_back.movie.project.dashboard_models import (
    Location,
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
from w_craft_back.movie.reference_library.providers import (
    DeterministicReferenceMockProvider,
)
from w_craft_back.movie.reference_library.worker import execute_reference_job


def make_user(username: str) -> tuple[User, str]:
    user = User.objects.create_user(username=username, password="pw")
    key = UserKey.objects.create(user=user)
    return user, str(key.key)


def make_project(owner: User) -> Project:
    project = Project.objects.create(
        owner=owner,
        title="Reference film",
        format="feature_film",
        annotation="",
        synopsis="",
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


class RegistryReferenceStubProvider(DeterministicReferenceMockProvider):
    """Exercise the registry worker path without making a network request."""

    name = "gemini-flash-image"
    model_id = "gemini/gemini-2.5-flash-image"


class OpenRouterReferenceStubProvider(DeterministicReferenceMockProvider):
    """Represent the configured OpenRouter image route without network I/O."""

    name = "openrouter-flash-image"
    model_id = "google/gemini-3.1-flash-image-preview"


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

    @override_settings(REFERENCE_IMAGE_PROVIDER="registry")
    def test_capabilities_model_key_can_be_priced_by_generation_estimate(self):
        self.project.generation_settings = {
            "image_generation_model": "openrouter-flash-image",
        }
        self.project.save(update_fields=["generation_settings"])
        provider = OpenRouterReferenceStubProvider()

        with patch(
            (
                "w_craft_back.movie.reference_library.services."
                "resolve_reference_provider"
            ),
            return_value=provider,
        ):
            capabilities = self.client.get(
                f"{self.collection_url}capabilities/",
                HTTP_X_USER_TOKEN=self.owner_token,
            )

        self.assertEqual(capabilities.status_code, 200, capabilities.content)
        effective_model = capabilities.json()["generation"]["effectiveModel"]
        self.assertEqual(effective_model, "openrouter-flash-image")

        estimate = self.client.post(
            "/api/credits/generation-estimate/",
            {
                "domain": "reference",
                "operation": "generate",
                "modelKey": effective_model,
                "variantCount": 1,
                "promptLength": 120,
                "resolution": "1K",
                "routingMode": "manual",
            },
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(estimate.status_code, 200, estimate.content)
        self.assertEqual(estimate.json()["modelKey"], "openrouter-flash-image")
        self.assertEqual(estimate.json()["estimatedCost"], "0.067015")
        self.assertEqual(
            estimate.json()["modelName"],
            "google/gemini-3.1-flash-image-preview",
        )

    def test_link_options_and_reference_settings_are_project_scoped(self):
        anna = StudioCharacter.objects.create(
            project=self.project,
            user=UserKey.objects.get(user=self.owner),
            name="Anna",
        )
        apartment = Location.objects.create(
            project=self.project,
            name="Anna's apartment",
        )
        other_project = make_project(self.outsider)
        StudioCharacter.objects.create(
            project=other_project,
            user=UserKey.objects.get(user=self.outsider),
            name="Outsider",
        )
        Location.objects.create(project=other_project, name="Foreign location")

        options = self.client.get(
            f"{self.collection_url}link-options/",
            HTTP_X_USER_TOKEN=self.viewer_token,
        )
        self.assertEqual(options.status_code, 200, options.content)
        self.assertEqual(
            options.json(),
            {
                "characters": [{"id": str(anna.character_id), "name": "Anna"}],
                "locations": [{"id": apartment.id, "name": "Anna's apartment"}],
            },
        )

        created = self.client.post(
            self.collection_url,
            {
                "title": "Anna's medallion",
                "category": "prop",
                "description": "Old silver medallion with red enamel",
                "brief": {
                    "schemaVersion": "reference_brief.v1",
                    "aspectRatio": "1:1",
                    "materials": ["silver", "enamel"],
                },
                "tags": [],
                "locationId": apartment.id,
                "characterLinks": [
                    {
                        "characterId": str(anna.character_id),
                        "relation": "associated",
                    }
                ],
            },
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["category"], "prop")
        self.assertEqual(created.json()["locationId"], apartment.id)
        self.assertEqual(
            created.json()["characterLinks"][0]["characterId"],
            str(anna.character_id),
        )

        upload = self.client.post(
            f"{self.collection_url}{created.json()['id']}/versions/upload/",
            {
                "file": png_upload("medallion.png"),
                "expectedReferenceVersion": created.json()["version"],
                "rightsConfirmed": True,
                "rightsStatementVersion": "reference-upload-v1",
            },
            format="multipart",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(upload.status_code, 201, upload.content)
        detail = self.client.get(
            f"{self.collection_url}{created.json()['id']}/",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(
            detail.json()["description"],
            "Old silver medallion with red enamel",
        )
        self.assertEqual(detail.json()["brief"]["materials"], ["silver", "enamel"])
        self.assertIsNotNone(detail.json()["activeVersion"]["imageUrl"])

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

    @override_settings(REFERENCE_IMAGE_PROVIDER="registry")
    def test_registry_generation_persists_generated_image_version(self):
        CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("1.00"),
        )
        created = self.create_reference()
        jobs_url = f"{self.collection_url}{created['id']}/generation-jobs/"
        provider = RegistryReferenceStubProvider()
        with (
            patch(
                (
                    "w_craft_back.movie.reference_library.services."
                    "resolve_reference_provider"
                ),
                return_value=provider,
            ),
            patch(
                (
                    "w_craft_back.movie.reference_library.worker."
                    "resolve_pinned_reference_provider"
                ),
                return_value=provider,
            ),
        ):
            enqueued = self.client.post(
                jobs_url,
                {
                    "expectedReferenceVersion": created["version"],
                    "operation": "generate",
                    "variantCount": 1,
                    "imageModel": "gemini-flash-image",
                    "brief": {
                        "schemaVersion": "reference_brief.v1",
                        "aspectRatio": "1:1",
                        "description": "A production reference for a red medallion",
                    },
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="registry-reference-flow",
                HTTP_X_USER_TOKEN=self.owner_token,
            )
            self.assertEqual(enqueued.status_code, 202, enqueued.content)
            job = execute_reference_job(enqueued.json()["id"])

        self.assertEqual(job.status, ReferenceJobStatus.COMPLETED)
        self.assertEqual(job.provider, provider.name)
        self.assertEqual(job.model_name, provider.model_id)
        variant = job.variants.get()
        applied = self.client.post(
            f"{jobs_url}{job.id}/variants/{variant.id}/apply/",
            {"expectedReferenceVersion": created["version"]},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(applied.status_code, 201, applied.content)
        reference = ProjectReference.objects.get(pk=created["id"])
        self.assertEqual(reference.active_version.provider, provider.name)
        self.assertEqual(reference.active_version.model_name, provider.model_id)

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

    def test_processing_job_cannot_be_cancelled(self):
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
                    "description": "A paid reference",
                },
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="processing-cancel-blocked",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        job_id = enqueued.json()["id"]
        ReferenceGenerationJob.objects.filter(pk=job_id).update(
            status=ReferenceJobStatus.PROCESSING,
        )

        detail = self.client.get(
            f"{jobs_url}{job_id}/",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        cancelled = self.client.post(
            f"{jobs_url}{job_id}/cancellation-request/",
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )

        self.assertFalse(detail.json()["canCancel"])
        self.assertEqual(cancelled.status_code, 409, cancelled.content)
        self.assertEqual(cancelled.json()["code"], "REFERENCE_JOB_NOT_CANCELLABLE")

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

        for index in range(5):
            ProjectReference.objects.create(
                project=self.project,
                title=f"Draft {index}",
                category="prop",
                created_by=self.owner,
                updated_by=self.owner,
            )
        ready = self.client.get(
            self.collection_url,
            {"status": "ready", "pageSize": 1},
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(ready.status_code, 200, ready.content)
        self.assertEqual(ready.json()["total"], 1)
        self.assertEqual(ready.json()["items"][0]["id"], created["id"])

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
