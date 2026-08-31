from __future__ import annotations

import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.credits.models import (
    CreditAccount,
    GenerationCharge,
    GenerationChargeStatus,
)
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
    Scene,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.reference_library.models import (
    ProjectReference,
    ReferenceCategory,
)
from w_craft_back.movie.reference_library.providers import (
    DeterministicReferenceMockProvider,
)
from w_craft_back.movie.storyboard.models import (
    CameraIntent,
    StoryboardKeyframe,
    StoryboardKeyframeGeneration,
)
from w_craft_back.movie.storyboard.lifecycle import (
    claim_storyboard_generation,
    fail_storyboard_generation,
    mark_storyboard_provider_started,
    mark_storyboard_provider_result_received,
)
from w_craft_back.movie.storyboard.worker import execute_storyboard_generation
from w_craft_back.services.image_generation.errors import ImageProviderError


def make_user(username: str) -> tuple[User, UserKey]:
    user = User.objects.create_user(username=username, password="pw")
    return user, UserKey.objects.create(user=user)


def make_project(owner: User, title: str) -> Project:
    project = Project.objects.create(
        owner=owner,
        title=title,
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


@override_settings(
    REFERENCE_IMAGE_PROVIDER="mock",
    REFERENCE_ALLOW_MOCK=True,
    ENVIRONMENT="development",
)
class StoryboardApiTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.client = APIClient()
        self.owner, self.owner_key = make_user("storyboard-owner")
        self.viewer, self.viewer_key = make_user("storyboard-viewer")
        self.outsider, self.outsider_key = make_user("storyboard-outsider")
        self.project = make_project(self.owner, "Storyboard film")
        self.other_project = make_project(self.outsider, "Other film")
        ProjectMember.objects.create(
            project=self.project,
            user=self.viewer,
            role=ProjectMemberRole.VIEWER,
        )
        CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("100"),
        )
        self.scene = Scene.objects.create(
            project=self.project,
            title="Kitchen",
            order=1,
            script_text="Anna enters the kitchen and sees an envelope.",
            created_by=self.owner,
            updated_by=self.owner,
        )
        self.character = StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Anna",
        )
        self.foreign_character = StudioCharacter.objects.create(
            project=self.other_project,
            user=self.outsider_key,
            name="Mallory",
        )
        self.reference = ProjectReference.objects.create(
            project=self.project,
            title="Envelope",
            category=ReferenceCategory.PROP,
            created_by=self.owner,
            updated_by=self.owner,
        )

    def token(self, key: UserKey | None = None) -> dict[str, str]:
        return {"HTTP_X_USER_TOKEN": str((key or self.owner_key).key)}

    def initialize(self) -> dict:
        response = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/scenes/{self.scene.id}/",
            {},
            format="json",
            **self.token(),
        )
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()

    def create_shot(self) -> dict:
        storyboard = self.initialize()
        response = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/"
            f"{storyboard['id']}/shots/",
            {
                "title": "Anna notices the envelope",
                "description": "Medium shot of Anna noticing the envelope.",
                "durationSeconds": "4.00",
                "characterIds": [str(self.character.pk)],
                "visualReferences": [
                    {
                        "referenceId": str(self.reference.pk),
                        "role": "object",
                    }
                ],
            },
            format="json",
            **self.token(),
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def camera_payload(self) -> dict:
        return {
            "target": {"type": "character", "ids": [str(self.character.pk)]},
            "azimuth": "front_left",
            "elevation": "eye_level",
            "distance": "medium",
            "framing": "medium_close",
            "lensMm": 50,
            "composition": [
                {
                    "subject_type": "character",
                    "subject_id": str(self.character.pk),
                    "x": 0.55,
                    "y": 0.1,
                    "width": 0.3,
                    "height": 0.8,
                }
            ],
        }

    @patch("w_craft_back.movie.storyboard.views.AIShotListService")
    def test_shot_list_endpoint_forwards_selected_model(self, service_class):
        service_class.return_value.suggest.return_value = {"shots": []}
        self.scene.script_text = "First sentence. " + "x" * 20000 + " Last sentence."
        self.scene.version = 7
        self.scene.save(update_fields=["script_text", "version"])

        response = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/scenes/"
            f"{self.scene.id}/suggest-shots/",
            {
                "model": "gemini/gemini-2.5-flash",
                "maxShots": 8,
            },
            format="json",
            **self.token(),
        )

        self.assertEqual(response.status_code, 200, response.content)
        service_class.assert_called_once_with(model="gemini/gemini-2.5-flash")
        service_class.return_value.suggest.assert_called_once()
        self.assertEqual(
            service_class.return_value.suggest.call_args.kwargs["max_shots"],
            8,
        )
        source = service_class.return_value.suggest.call_args.kwargs["source"]
        self.assertEqual(source["scene_id"], self.scene.id)
        self.assertEqual(source["scene_version"], self.scene.version)
        self.assertTrue(source["truncated"])
        self.assertEqual(
            "".join(segment["text"] for segment in source["segments"]),
            self.scene.script_text.strip(),
        )

    @patch("w_craft_back.movie.storyboard.views.AIShotListService")
    def test_shot_list_cannot_disclose_source_from_another_project(self, service_class):
        response = self.client.post(
            f"/api/projects/{self.other_project.id}/storyboard/scenes/"
            f"{self.scene.id}/suggest-shots/",
            {"maxShots": 8}, format="json", **self.token(self.outsider_key),
        )
        self.assertEqual(response.status_code, 404)
        service_class.assert_not_called()

    def test_shot_creation_adds_boundaries_and_transition(self):
        shot = self.create_shot()

        self.assertEqual(
            [item["type"] for item in shot["keyframes"]],
            ["start", "end"],
        )
        self.assertEqual(
            [item["position"] for item in shot["keyframes"]],
            [0.0, 1.0],
        )
        self.assertEqual(len(shot["transitions"]), 1)
        self.assertEqual(shot["transitions"][0]["detectedMovement"], "custom")
        self.assertFalse(shot["readiness"]["ready"])

    def test_project_scope_rejects_foreign_character_and_object_lookup(self):
        storyboard = self.initialize()
        denied = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/"
            f"{storyboard['id']}/shots/",
            {"characterIds": [str(self.foreign_character.pk)]},
            format="json",
            **self.token(),
        )
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(denied.json()["code"], "STORYBOARD_FOREIGN_CHARACTER")

        shot = self.create_shot()
        hidden = self.client.get(
            f"/api/projects/{self.other_project.id}/storyboard/shots/{shot['id']}/",
            **self.token(self.outsider_key),
        )
        self.assertEqual(hidden.status_code, 404)

    def test_intermediate_keyframe_rules_and_boundary_protection(self):
        shot = self.create_shot()
        added = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/shots/"
            f"{shot['id']}/keyframes/",
            {"position": "0.4500"},
            format="json",
            **self.token(),
        )
        self.assertEqual(added.status_code, 201, added.content)
        self.assertEqual(added.json()["type"], "intermediate")
        start_id = shot["keyframes"][0]["id"]
        protected = self.client.delete(
            f"/api/projects/{self.project.id}/storyboard/keyframes/{start_id}/",
            **self.token(),
        )
        self.assertEqual(protected.status_code, 400)
        deleted = self.client.delete(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{added.json()['id']}/",
            **self.token(),
        )
        self.assertEqual(deleted.status_code, 204)

    def test_camera_validation_rejects_foreign_target(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        payload = self.camera_payload()
        payload["target"] = {
            "type": "character",
            "ids": [str(self.foreign_character.pk)],
        }
        response = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            payload,
            format="json",
            **self.token(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "STORYBOARD_VALIDATION_ERROR")

    def test_ots_metadata_rejects_foreign_project_characters(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        payload = self.camera_payload()
        payload.update(
            {
                "framing": "ots",
                "cameraMetadata": {
                    "foreground_subject_id": str(self.foreign_character.pk),
                    "target_subject_id": str(self.character.pk),
                    "shoulder": "left",
                },
            }
        )
        response = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            payload,
            format="json",
            **self.token(),
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["code"], "STORYBOARD_VALIDATION_ERROR")

    def test_camera_update_requires_version_and_valid_composition(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        url = (
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/"
        )
        created = self.client.put(
            url,
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(created.status_code, 200, created.content)

        missing_version = self.client.put(
            url,
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(missing_version.status_code, 400)
        self.assertEqual(
            missing_version.json()["code"],
            "STORYBOARD_VERSION_REQUIRED",
        )

        invalid = self.camera_payload()
        invalid["expectedVersion"] = 1
        invalid["composition"][0]["x"] = 0.9
        invalid["composition"][0]["width"] = 0.2
        invalid_response = self.client.put(
            url,
            invalid,
            format="json",
            **self.token(),
        )
        self.assertEqual(invalid_response.status_code, 400)
        self.assertEqual(
            invalid_response.json()["code"],
            "STORYBOARD_VALIDATION_ERROR",
        )

    def test_generation_references_enforce_target_type_and_reject_self(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        url = (
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/references/"
        )
        mismatch = self.client.put(
            url,
            {
                "references": [
                    {
                        "referenceType": "character",
                        "visualReferenceId": str(self.reference.pk),
                    }
                ]
            },
            format="json",
            **self.token(),
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch.json()["code"], "STORYBOARD_INVALID_REFERENCE")

        self_reference = self.client.put(
            url,
            {
                "references": [
                    {
                        "referenceType": "previous_keyframe",
                        "sourceKeyframeId": start_id,
                    }
                ]
            },
            format="json",
            **self.token(),
        )
        self.assertEqual(self_reference.status_code, 400)
        self.assertEqual(
            self_reference.json()["code"],
            "STORYBOARD_INVALID_REFERENCE",
        )

        too_large = self.client.put(
            url,
            {
                "references": [
                    {
                        "referenceType": "object",
                        "visualReferenceId": str(self.reference.pk),
                        "priority": 32768,
                    }
                ]
            },
            format="json",
            **self.token(),
        )
        self.assertEqual(too_large.status_code, 400, too_large.content)

    def test_generation_is_async_idempotent_and_worker_selects_revision(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        camera = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(camera.status_code, 200, camera.content)
        generation_url = (
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/generate/"
        )
        queued = self.client.post(
            generation_url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="start-v1",
            **self.token(),
        )
        replayed = self.client.post(
            generation_url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="start-v1",
            **self.token(),
        )
        self.assertEqual(queued.status_code, 201, queued.content)
        self.assertEqual(replayed.status_code, 200, replayed.content)
        self.assertEqual(
            queued.json()["generationId"],
            replayed.json()["generationId"],
        )
        self.assertEqual(queued.json()["status"], "queued")

        result = execute_storyboard_generation(queued.json()["generationId"])

        self.assertEqual(result.status, "ready")
        keyframe = StoryboardKeyframe.objects.get(pk=start_id)
        self.assertEqual(keyframe.current_generation_id, result.pk)
        poll = self.client.get(
            f"/api/projects/{self.project.id}/storyboard/generations/"
            f"{result.pk}/",
            **self.token(),
        )
        self.assertEqual(poll.status_code, 200, poll.content)
        self.assertEqual(poll.json()["status"], "ready")
        self.assertIn("/api/media/", poll.json()["imageUrl"])
        workspace = self.client.get(
            f"/api/projects/{self.project.id}/storyboard/scenes/"
            f"{self.scene.id}/",
            **self.token(),
        )
        self.assertEqual(workspace.status_code, 200, workspace.content)
        self.assertFalse(
            workspace.json()["shots"][0]["keyframes"][0]["image"]["outdated"]
        )

        storyboard = self.initialize()
        reordered = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/"
            f"{storyboard['id']}/shots/reorder/",
            {"shotIds": [shot["id"]]},
            format="json",
            **self.token(),
        )
        self.assertEqual(reordered.status_code, 200, reordered.content)
        self.assertIn(
            "/api/media/",
            reordered.json()["shots"][0]["keyframes"][0]["image"]["url"],
        )

        changed_camera = self.camera_payload()
        changed_camera["expectedVersion"] = 1
        changed_camera["azimuth"] = "right"
        changed = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            changed_camera,
            format="json",
            **self.token(),
        )
        self.assertEqual(changed.status_code, 200, changed.content)
        workspace = self.client.get(
            f"/api/projects/{self.project.id}/storyboard/scenes/"
            f"{self.scene.id}/",
            **self.token(),
        ).json()
        keyframe_payload = workspace["shots"][0]["keyframes"][0]
        self.assertEqual(workspace["status"], "draft")
        self.assertTrue(keyframe_payload["image"]["outdated"])
        self.assertIsNone(
            StoryboardKeyframe.objects.get(pk=start_id).current_generation_id
        )

        revision_two = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/regenerate/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="start-v2",
            **self.token(),
        )
        self.assertEqual(revision_two.status_code, 201, revision_two.content)
        workspace = self.client.get(
            f"/api/projects/{self.project.id}/storyboard/scenes/"
            f"{self.scene.id}/",
            **self.token(),
        ).json()
        keyframe_payload = workspace["shots"][0]["keyframes"][0]
        self.assertEqual(keyframe_payload["image"]["id"], str(result.pk))
        self.assertEqual(
            keyframe_payload["latestGeneration"]["id"],
            revision_two.json()["generationId"],
        )
        self.assertEqual(
            keyframe_payload["activeGeneration"]["id"],
            revision_two.json()["generationId"],
        )

    def test_idempotency_rejects_changed_generation_options(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        camera = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(camera.status_code, 200, camera.content)
        url = (
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/generate/"
        )
        queued = self.client.post(
            url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="same-key-different-options",
            **self.token(),
        )
        self.assertEqual(queued.status_code, 201, queued.content)
        mismatch = self.client.post(
            url,
            {"imageModel": "different-model"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="same-key-different-options",
            **self.token(),
        )
        self.assertEqual(mismatch.status_code, 409, mismatch.content)
        self.assertEqual(
            mismatch.json()["code"],
            "STORYBOARD_IDEMPOTENCY_MISMATCH",
        )
        second_key = self.client.post(
            url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="other-active-key",
            **self.token(),
        )
        self.assertEqual(second_key.status_code, 409, second_key.content)
        self.assertEqual(
            second_key.json()["code"],
            "STORYBOARD_GENERATION_ACTIVE",
        )

    def test_patch_rejects_duplicate_relations(self):
        shot = self.create_shot()
        response = self.client.patch(
            f"/api/projects/{self.project.id}/storyboard/shots/{shot['id']}/",
            {
                "expectedVersion": shot["version"],
                "visualReferences": [
                    {
                        "referenceId": str(self.reference.pk),
                        "role": "object",
                    },
                    {
                        "referenceId": str(self.reference.pk),
                        "role": "object",
                    },
                ],
            },
            format="json",
            **self.token(),
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_failure_settlement_depends_on_provider_result(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        camera = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(camera.status_code, 200, camera.content)
        url = (
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/generate/"
        )

        queued = self.client.post(
            url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="failure-before-provider",
            **self.token(),
        ).json()
        claimed = claim_storyboard_generation(queued["generationId"])
        self.assertTrue(
            fail_storyboard_generation(
                claimed,
                code="TEST_PRE_PROVIDER",
                detail="provider not called",
            )
        )
        charge = GenerationCharge.objects.get(
            domain="storyboard",
            job_id=queued["generationId"],
        )
        self.assertEqual(charge.status, GenerationChargeStatus.RELEASED)

        queued = self.client.post(
            url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="failure-after-provider",
            **self.token(),
        ).json()
        claimed = claim_storyboard_generation(queued["generationId"])
        mark_storyboard_provider_started(claimed)
        mark_storyboard_provider_result_received(claimed)
        self.assertTrue(
            fail_storyboard_generation(
                claimed,
                code="TEST_UNKNOWN_PROVIDER_OUTCOME",
                detail="provider may have completed",
            )
        )
        charge = GenerationCharge.objects.get(
            domain="storyboard",
            job_id=queued["generationId"],
        )
        self.assertEqual(charge.status, GenerationChargeStatus.CAPTURED)
        self.assertTrue(charge.cost_is_estimate)
        self.assertEqual(charge.actual_cost, charge.reserved_amount)

    def test_known_provider_rejection_releases_but_timeout_captures(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        camera = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(camera.status_code, 200, camera.content)
        url = (
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/generate/"
        )

        class RejectingProvider:
            name = "mock"
            model_id = "reference-mock-v1"

            def __init__(self, *, timeout):
                self.http_status = timeout

            def generate(self, *args, **kwargs):
                raise ImageProviderError(
                    code=(
                        "IMAGE_PROVIDER_UNAVAILABLE"
                        if self.http_status == 504
                        else "IMAGE_PROVIDER_BLOCKED"
                    ),
                    message="known rejection or timeout",
                    http_status=self.http_status,
                )

        blocked = self.client.post(
            url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="known-provider-rejection",
            **self.token(),
        ).json()
        with patch(
            "w_craft_back.movie.storyboard.worker._provider_for_generation",
            return_value=RejectingProvider(timeout=400),
        ):
            execute_storyboard_generation(blocked["generationId"])
        blocked_charge = GenerationCharge.objects.get(
            domain="storyboard",
            job_id=blocked["generationId"],
        )
        self.assertEqual(
            blocked_charge.status,
            GenerationChargeStatus.RELEASED,
        )

        timed_out = self.client.post(
            url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="unknown-provider-timeout",
            **self.token(),
        ).json()
        with patch(
            "w_craft_back.movie.storyboard.worker._provider_for_generation",
            return_value=RejectingProvider(timeout=504),
        ):
            execute_storyboard_generation(timed_out["generationId"])
        timeout_charge = GenerationCharge.objects.get(
            domain="storyboard",
            job_id=timed_out["generationId"],
        )
        self.assertEqual(
            timeout_charge.status,
            GenerationChargeStatus.CAPTURED,
        )

    def test_missing_local_reference_releases_before_provider_start(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        camera = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(camera.status_code, 200, camera.content)
        queued = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/generate/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="missing-local-reference",
            **self.token(),
        ).json()
        StoryboardKeyframeGeneration.objects.filter(
            pk=queued["generationId"],
        ).update(
            request_snapshot={
                "compiledPrompt": "Storyboard with missing local reference",
                "primary_reference": {
                    "storageKey": "missing/storyboard-reference.png",
                    "mimeType": "image/png",
                },
            }
        )

        class ReferenceProvider:
            name = "mock"
            model_id = "reference-mock-v1"

            def generate_with_reference(self, *args, **kwargs):
                raise AssertionError("provider must not be called")

        with patch(
            "w_craft_back.movie.storyboard.worker._provider_for_generation",
            return_value=ReferenceProvider(),
        ):
            execute_storyboard_generation(queued["generationId"])

        generation = StoryboardKeyframeGeneration.objects.get(
            pk=queued["generationId"],
        )
        charge = GenerationCharge.objects.get(
            domain="storyboard",
            job_id=queued["generationId"],
        )
        self.assertIsNone(generation.provider_started_at)
        self.assertEqual(charge.status, GenerationChargeStatus.RELEASED)

    def test_finalize_does_not_select_result_after_midflight_camera_edit(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        camera = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(camera.status_code, 200, camera.content)
        queued = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/generate/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="midflight-camera-edit",
            **self.token(),
        ).json()

        class EditingProvider(DeterministicReferenceMockProvider):
            def generate(provider_self, *args, **kwargs):
                CameraIntent.objects.filter(keyframe_id=start_id).update(
                    azimuth="right",
                    version=2,
                )
                return super().generate(*args, **kwargs)

        with patch(
            "w_craft_back.movie.storyboard.worker._provider_for_generation",
            return_value=EditingProvider(),
        ):
            result = execute_storyboard_generation(queued["generationId"])

        self.assertEqual(result.status, "ready")
        self.assertIsNone(
            StoryboardKeyframe.objects.get(pk=start_id).current_generation_id
        )

    def test_provider_configuration_errors_keep_public_status(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        camera = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(camera.status_code, 200, camera.content)
        url = (
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/generate/"
        )

        with self.settings(REFERENCE_IMAGE_PROVIDER="invalid"):
            invalid = self.client.post(
                url,
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="invalid-provider-mode",
                **self.token(),
            )
        self.assertEqual(invalid.status_code, 503, invalid.content)
        self.assertEqual(invalid.json()["code"], "IMAGE_PROVIDER_NOT_CONFIGURED")

        with self.settings(ENVIRONMENT="production", REFERENCE_ALLOW_MOCK=False):
            disabled = self.client.post(
                url,
                {},
                format="json",
                HTTP_IDEMPOTENCY_KEY="disabled-production-mock",
                **self.token(),
            )
        self.assertEqual(disabled.status_code, 503, disabled.content)
        self.assertEqual(disabled.json()["code"], "IMAGE_PROVIDER_NOT_CONFIGURED")

    def test_scene_cascade_releases_queued_generation(self):
        shot = self.create_shot()
        start_id = shot["keyframes"][0]["id"]
        camera = self.client.put(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/camera-intent/",
            self.camera_payload(),
            format="json",
            **self.token(),
        )
        self.assertEqual(camera.status_code, 200, camera.content)
        queued = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/keyframes/"
            f"{start_id}/generate/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="queued-scene-delete",
            **self.token(),
        ).json()

        self.scene.delete()

        charge = GenerationCharge.objects.get(
            domain="storyboard",
            job_id=queued["generationId"],
        )
        self.assertEqual(charge.status, GenerationChargeStatus.RELEASED)

    def test_duplicate_and_reorder_keep_settings_without_images(self):
        first = self.create_shot()
        storyboard = self.initialize()
        second_response = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/"
            f"{storyboard['id']}/shots/",
            {"title": "Second shot"},
            format="json",
            **self.token(),
        )
        self.assertEqual(
            second_response.status_code,
            201,
            second_response.content,
        )
        second = second_response.json()

        duplicate = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/shots/"
            f"{first['id']}/duplicate/",
            {},
            format="json",
            **self.token(),
        )
        self.assertEqual(duplicate.status_code, 201, duplicate.content)
        self.assertEqual(duplicate.json()["title"], first["title"])
        self.assertTrue(all(
            item["image"]["status"] == "empty"
            for item in duplicate.json()["keyframes"]
        ))

        shot_ids = [duplicate.json()["id"], second["id"], first["id"]]
        reordered = self.client.post(
            f"/api/projects/{self.project.id}/storyboard/"
            f"{storyboard['id']}/shots/reorder/",
            {"shotIds": shot_ids},
            format="json",
            **self.token(),
        )
        self.assertEqual(reordered.status_code, 200, reordered.content)
        self.assertEqual(
            [item["id"] for item in reordered.json()["shots"]],
            shot_ids,
        )

    def test_completed_status_requires_start_and_end_camera_and_images(self):
        shot = self.create_shot()
        for item in shot["keyframes"]:
            keyframe = StoryboardKeyframe.objects.get(pk=item["id"])
            CameraIntent.objects.create(
                keyframe=keyframe,
                target={
                    "type": "character",
                    "ids": [str(self.character.pk)],
                },
            )
            generation = StoryboardKeyframeGeneration.objects.create(
                keyframe=keyframe,
                actor=self.owner,
                request_snapshot={"keyframe": str(keyframe.pk)},
                request_fingerprint=str(keyframe.pk).replace("-", "")[:64],
                status="ready",
            )
            keyframe.current_generation = generation
            keyframe.save(update_fields=["current_generation", "updated_at"])
        response = self.client.get(
            f"/api/projects/{self.project.id}/storyboard/scenes/{self.scene.id}/",
            **self.token(),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(response.json()["readyShotsCount"], 1)
