from __future__ import annotations

import tempfile
import uuid
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import Mock, patch

from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from w_craft_back.character_studio.models import CharacterAsset, StudioCharacter
from w_craft_back.credits.models import CreditAccount, GenerationCharge
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember, ProjectMemberRole, Scene,
)
from w_craft_back.movie.storyboard.canvas_render import render_canvas
from w_craft_back.movie.storyboard.editor_drafts import (
    CanvasDocumentSerializer, save_editor_draft,
)
from w_craft_back.movie.storyboard.editor_frames import (
    _boundary, claim_frame_job, execute_frame_job, recover_stale_frame_jobs,
)
from w_craft_back.movie.storyboard.lifecycle import StoryboardLeaseLost
from w_craft_back.movie.storyboard.models import (
    SceneStoryboardEditorDraft, StoryboardEditorFrameJob, StoryboardShot,
)
from w_craft_back.movie.storyboard.tests.test_api import make_project, make_user
from w_craft_back.movie.storyboard.tests.test_editor_drafts import make_payload
from w_craft_back.services.image_generation.errors import ImageProviderError
from w_craft_back.services.image_generation.registry import MODEL_REGISTRY, ModelSpec
from w_craft_back.storage_gateway import store_normalized_image, normalize_image_bytes


MODULE = "w_craft_back.movie.storyboard.editor_frames"


def canvas_document():
    return {
        "version": 1, "aspectRatio": "16:9",
        "objects": [{
            "id": "person-1", "kind": "person", "x": 20, "y": 10,
            "width": 15, "height": 65, "rotation": 0, "flipX": False,
            "hidden": False, "locked": False, "title": "Анна",
            "description": "Смотрит вправо", "pose": "front", "comment": "",
            "motion": {"type": "static", "points": [], "start": 0,
                       "end": 4, "facing": ""},
        }],
        "cameraMotion": {"type": "Static", "intensity": "low", "start": 0, "end": 4},
        "lighting": {"preset": "daylight", "direction": "top-left", "softness": "soft",
                     "temperature": "neutral", "contrast": "low", "notes": ""},
        "notes": "", "markers": [],
    }


def png():
    output = BytesIO()
    Image.new("RGB", (80, 40), "green").save(output, format="PNG")
    return output.getvalue()


class CanvasValidationTests(SimpleTestCase):
    def test_limits_geometry_and_unknown_fields(self):
        self.assertTrue(CanvasDocumentSerializer(data=canvas_document()).is_valid())
        cases = (("width", float("inf")), ("x", -1),
                 ("rotation", 361), ("imageUrl", "https://example.com/a.png"))
        for field, value in cases:
            document = canvas_document()
            document["objects"][0][field] = value
            self.assertFalse(CanvasDocumentSerializer(data=document).is_valid())
        document = canvas_document()
        document["objects"] *= 81
        self.assertFalse(CanvasDocumentSerializer(data=document).is_valid())

    def test_accepts_and_limits_custom_camera_trajectory(self):
        document = canvas_document()
        document["cameraMotion"].update({
            "type": "Custom",
            "points": [{"x": 10, "y": 70}, {"x": 45, "y": 25}, {"x": 80, "y": 50}],
        })
        serializer = CanvasDocumentSerializer(data=document)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(len(serializer.validated_data["cameraMotion"]["points"]), 3)
        document["cameraMotion"]["points"] *= 3
        self.assertFalse(CanvasDocumentSerializer(data=document).is_valid())

    def test_annotations_do_not_appear_in_condition_and_geometry_changes_it(self):
        document = canvas_document()
        expected = render_canvas(document)
        document["markers"] = [{"id": "note-1", "x": 20, "y": 30, "text": "Note"}]
        document["notes"] = "Hidden annotation"
        document["objects"][0]["motion"]["points"] = [{"x": 50, "y": 20}]
        self.assertEqual(render_canvas(document), expected)
        document["objects"][0]["x"] = 50
        self.assertNotEqual(render_canvas(document), expected)
        image = Image.open(BytesIO(expected))
        self.assertEqual(image.size, (1024, 576))


@override_settings(STORYBOARD_SHOT_LIST_THROTTLE_RATE="1000/min")
class EditorFrameJobTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner, cls.owner_key = make_user("editor-image-owner")
        cls.viewer, cls.viewer_key = make_user("editor-image-viewer")
        cls.outsider, cls.outsider_key = make_user("editor-image-outsider")
        cls.project = make_project(cls.owner, "Film")
        cls.other = make_project(cls.outsider, "Other")
        ProjectMember.objects.create(
            project=cls.project, user=cls.viewer, role=ProjectMemberRole.VIEWER,
        )
        cls.scene = Scene.objects.create(
            project=cls.project, title="Room", order=1, script_text="Анна входит",
        )
        CreditAccount.objects.create(user=cls.owner, available_balance=Decimal("10"))

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.client = APIClient()
        self.url = (f"/api/projects/{self.project.pk}/storyboard/scenes/"
                    f"{self.scene.pk}/editor-frame-jobs/")
        self.payload = make_payload(self.scene)
        self.frame = self.payload["shots"][0]["keyframes"][0]
        self.frame["generationReferences"] = []
        self.frame["canvas"] = canvas_document()
        self.save()
        self.provider = Mock()
        self.provider.spec = MODEL_REGISTRY["openrouter-flash-image"]
        self.provider.name = self.provider.spec.backend
        self.provider.model_id = self.provider.spec.model_id
        self.provider.last_usage = {}
        self.provider.generate.return_value = [png()]
        self.provider.generate_with_reference.return_value = [png()]
        self.provider.generate_with_references.return_value = [png()]
        for name in ("resolve_provider_for_user", "provider_from_spec"):
            mocked = patch(f"{MODULE}.{name}", return_value=self.provider)
            mocked.start()
            self.addCleanup(mocked.stop)

    def save(self):
        existing = SceneStoryboardEditorDraft.objects.filter(scene=self.scene).first()
        return save_editor_draft(
            actor=self.owner, project_id=self.project.pk,
            scene_id=self.scene.pk,
            expected_revision=existing.revision if existing else 0,
            mutation_id=uuid.uuid4(), payload=deepcopy(self.payload),
        )

    def post(self, **kwargs):
        key = kwargs.pop("key", self.owner_key)
        data = {"shotId": "shot-local-1", "keyframeId": "keyframe-1",
                "expectedRevision": 1, "imageModel": "openrouter-flash-image",
                "requestId": str(uuid.uuid4()), **kwargs}
        return self.client.post(
            self.url, data, format="json", HTTP_X_USER_TOKEN=str(key.key),
        )

    def jobs(self, key=None):
        token = str((key or self.owner_key).key)
        return self.client.get(self.url, HTTP_X_USER_TOKEN=token)

    def test_options_enable_priced_openrouter_models_and_choose_valid_default(self):
        qwen = ModelSpec(
            key="openrouter-images:qwen/qwen-image-3",
            label="Qwen Image 3",
            backend="openrouter-images",
            model_id="qwen/qwen-image-3",
            mode="images",
            supports_generate=True,
            supports_edit=True,
            supports_reference=True,
            supported_parameters={
                "input_references": {"type": "range", "min": 0, "max": 6},
            },
            provider_pricing={
                "source": "openrouter",
                "catalog": [{
                    "billable": "output_image",
                    "unit": "image",
                    "cost_usd": "0.03",
                }],
            },
        )
        catalog = [{
            "key": qwen.key,
            "configured": True,
            "supports_generate": True,
        }]
        options_url = (
            f"/api/projects/{self.project.pk}/storyboard/scenes/"
            f"{self.scene.pk}/editor-frame-options/"
        )
        with (
            patch(f"{MODULE}.list_available_models", return_value=catalog),
            patch(f"{MODULE}.resolve_model", return_value=qwen),
            patch(
                f"{MODULE}.resolve_current_for_user",
                return_value={"key": "unavailable-default"},
            ),
        ):
            response = self.client.get(
                options_url,
                HTTP_X_USER_TOKEN=str(self.owner_key.key),
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["defaultModel"], qwen.key)
        self.assertEqual(response.json()["models"], [{
            "id": qwen.key,
            "label": "Qwen Image 3",
            "available": True,
            "supportsReferences": True,
            "maxReferenceImages": 6,
            "estimatedCost": "0.030000",
            "currency": "USD",
        }])

    def test_job_survives_browser_and_cannot_revert_newer_or_reset_draft(self):
        response = self.post()
        self.assertEqual(response.status_code, 202, response.content)
        job_id = response.json()["jobId"]
        self.payload["shots"][0]["description"] = "Different action"
        self.save()
        result = execute_frame_job(job_id)
        self.assertEqual(result.status, "succeeded")
        job = self.jobs().json()["jobs"][0]
        self.assertTrue(job["imageUrl"])
        self.assertFalse(job["matchesCurrentDraft"])
        self.assertEqual(SceneStoryboardEditorDraft.objects.get().payload, self.payload)
        self.payload["shots"] = []
        self.save()
        self.assertEqual(self.jobs().json()["jobs"], [])
        self.assertTrue(StoryboardEditorFrameJob.objects.filter(pk=job_id).exists())
        self.assertEqual(StoryboardShot.objects.count(), 0)

    def test_generation_estimate_uses_recent_success_for_same_project_and_model(self):
        first = self.post()
        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(first.json()["estimatedSeconds"], 45)
        first_id = first.json()["jobId"]
        self.assertEqual(execute_frame_job(first_id).status, "succeeded")
        finished_at = timezone.now()
        StoryboardEditorFrameJob.objects.filter(pk=first_id).update(
            started_at=finished_at - timedelta(seconds=32),
            finished_at=finished_at,
        )

        second = self.post()

        self.assertEqual(second.status_code, 202, second.content)
        self.assertEqual(second.json()["estimatedSeconds"], 32)
        self.assertEqual(
            StoryboardEditorFrameJob.objects.get(
                pk=second.json()["jobId"],
            ).request_snapshot["estimatedSeconds"],
            32,
        )

    def test_idempotency_and_duplicate_active_requests(self):
        request_id = str(uuid.uuid4())
        first = self.post(requestId=request_id)
        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(self.post(requestId=request_id).json()["jobId"],
                         first.json()["jobId"])
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(
            self.post(requestId=request_id, expectedRevision=2).status_code, 400,
        )
        self.assertEqual(StoryboardEditorFrameJob.objects.count(), 1)
        self.assertEqual(GenerationCharge.objects.count(), 1)

    def test_permissions_and_revision_are_checked_before_billing(self):
        self.assertEqual(self.post(key=self.viewer_key).status_code, 403)
        self.assertEqual(self.post(key=self.outsider_key).status_code, 403)
        self.assertEqual(self.post(expectedRevision=2).status_code, 409)
        self.assertEqual(self.jobs(self.outsider_key).status_code, 403)
        self.assertEqual(GenerationCharge.objects.count(), 0)

    def test_transient_status_does_not_make_result_stale(self):
        job_id = self.post().json()["jobId"]
        execute_frame_job(job_id)
        self.frame["generationStatus"] = "ready"
        self.frame["canvas"]["objects"][0]["locked"] = True
        self.payload["stage"] = "editor"
        self.save()
        self.assertTrue(self.jobs().json()["jobs"][0]["matchesCurrentDraft"])

    def test_timeout_does_not_retry_paid_call_and_stale_worker_is_fenced(self):
        job_id = self.post().json()["jobId"]
        claimed = claim_frame_job(job_id)
        _boundary(claimed, "provider_started_at")
        StoryboardEditorFrameJob.objects.filter(pk=job_id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        recover_stale_frame_jobs()
        self.assertIsNone(execute_frame_job(job_id))
        job = StoryboardEditorFrameJob.objects.get(pk=job_id)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "IMAGE_PROVIDER_OUTCOME_UNKNOWN")
        self.assertEqual(GenerationCharge.objects.get().status, "captured")

    def test_known_provider_failure_releases_reservation(self):
        job_id = self.post().json()["jobId"]
        self.provider.generate_with_reference.side_effect = ImageProviderError(
            code="IMAGE_PROVIDER_AUTH", message="Authentication failed",
            http_status=401,
        )
        self.assertEqual(execute_frame_job(job_id).status, "failed")
        self.assertEqual(GenerationCharge.objects.get().status, "released")

    def test_paid_malformed_response_keeps_actual_charge(self):
        job_id = self.post().json()["jobId"]
        self.provider.usage_snapshot.return_value = {
            "calls": 1, "costUsd": "0.03125", "costSource": "provider",
        }
        self.provider.generate_with_reference.side_effect = ImageProviderError(
            code="IMAGE_PROVIDER_BAD_RESPONSE", message="Invalid image output",
            http_status=502, provider_status=200,
        )
        job = execute_frame_job(job_id)
        self.assertEqual(job.status, "failed")
        self.assertIsNotNone(job.provider_result_received_at)
        charge = GenerationCharge.objects.get()
        self.assertEqual(charge.status, "captured")
        self.assertEqual(charge.actual_cost, Decimal("0.03125"))
        self.assertFalse(charge.cost_is_estimate)

    def test_transport_timeout_is_not_confused_with_upstream_rejection(self):
        for provider_status, status in ((None, "captured"), (503, "released")):
            job_id = self.post().json()["jobId"]
            self.provider.generate_with_reference.side_effect = ImageProviderError(
                code="IMAGE_PROVIDER_UNAVAILABLE", message="Unavailable",
                http_status=503, provider_status=provider_status,
            )
            job = execute_frame_job(job_id)
            self.assertEqual(job.status, "failed")
            self.assertEqual(
                job.error_code,
                "IMAGE_PROVIDER_OUTCOME_UNKNOWN"
                if provider_status is None else "IMAGE_PROVIDER_UNAVAILABLE",
            )
            self.assertEqual(GenerationCharge.objects.get(job_id=job_id).status, status)

    def test_provider_boundaries_renew_live_lease_but_not_expired_lease(self):
        job_id = self.post().json()["jobId"]
        claimed = claim_frame_job(job_id)
        StoryboardEditorFrameJob.objects.filter(pk=job_id).update(
            lease_expires_at=timezone.now() + timedelta(seconds=2),
        )
        _boundary(claimed, "provider_started_at")
        self.assertGreater(claimed.lease_expires_at,
                           timezone.now() + timedelta(seconds=60))
        StoryboardEditorFrameJob.objects.filter(pk=job_id).update(
            lease_expires_at=timezone.now() + timedelta(seconds=2),
        )
        _boundary(claimed, "provider_result_received_at")
        self.assertGreater(claimed.lease_expires_at,
                           timezone.now() + timedelta(seconds=60))
        StoryboardEditorFrameJob.objects.filter(pk=job_id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=2),
        )
        with self.assertRaises(StoryboardLeaseLost):
            _boundary(claimed, "provider_result_received_at")

    def test_canvas_and_pinned_character_are_sent_together(self):
        character = StudioCharacter.objects.create(
            project=self.project, user=self.owner_key, name="Anna",
        )
        stored = store_normalized_image(
            normalize_image_bytes(png()), namespace="tests/editor",
        )
        asset = CharacterAsset.objects.create(
            character=character, project=self.project,
            storage_path=stored.storage_key, mime_type="image/png",
            asset_type="reference",
        )
        self.frame["canvas"]["objects"][0]["entity"] = {
            "id": str(character.pk), "type": "character", "title": "Anna",
            "assetId": str(asset.pk),
        }
        self.frame["canvas"]["objects"][0]["comment"] = "Legacy hidden instruction"
        second = deepcopy(self.frame["canvas"]["objects"][0])
        second["id"], second["title"], second["x"] = "person-2", "Anna reflection", 55
        self.frame["canvas"]["objects"].append(second)
        self.save()
        response = self.post(expectedRevision=2)
        self.assertEqual(response.status_code, 202, response.content)
        job = execute_frame_job(response.json()["jobId"])
        self.assertEqual(job.status, "succeeded")
        args = self.provider.generate_with_references.call_args.args
        self.assertEqual(len(args[1]), 2)
        self.assertIn('"image": 2', args[0])
        self.assertIn('"canvasObjectIds": ["person-1", "person-2"]', args[0])
        self.assertIn("intensity is the saved tempo", args[0])
        self.assertIn('"description": "Смотрит вправо"', args[0])
        self.assertNotIn("Legacy hidden instruction", args[0])
        self.assertNotIn('"comment":', args[0])
        self.assertIn("apply that numbered image to the exact canvas objects", args[0])
        self.assertEqual(job.input_assets.get().character_asset_id, asset.pk)

    def test_cross_project_reference_and_capability_mismatch_never_call_provider(self):
        character = StudioCharacter.objects.create(
            project=self.other, user=self.outsider_key, name="Other",
        )
        self.frame["canvas"]["objects"][0]["entity"] = {
            "id": str(character.pk), "type": "character", "title": "Other",
        }
        self.save()
        self.assertEqual(self.post(expectedRevision=2).status_code, 400)
        self.frame["canvas"]["objects"][0].pop("entity")
        self.save()
        self.provider.spec = MODEL_REGISTRY["gemini-imagen-4"]
        response = self.post(expectedRevision=3)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "STORYBOARD_REFERENCE_LIMIT")
        self.assertEqual(GenerationCharge.objects.count(), 0)
