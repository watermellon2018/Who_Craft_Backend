"""User-key execution, free text exemptions and pre-submit access boundaries."""

import json
import tempfile
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from w_craft_back.credits.models import CreditAccount, GenerationCharge
from w_craft_back.movie.project.dashboard_models import Scene
from w_craft_back.movie.storyboard.models import SceneStoryboardShotListJob
from w_craft_back.movie.storyboard.editor_drafts import save_editor_draft
from w_craft_back.movie.storyboard.editor_frames import execute_frame_job
from w_craft_back.movie.storyboard.shot_list_jobs import execute_shot_list_job
from w_craft_back.movie.storyboard.tests.test_api import make_project, make_user
from w_craft_back.movie.storyboard.tests.test_editor_drafts import make_payload
from w_craft_back.movie.storyboard.tests.test_editor_frames import png
from w_craft_back.movie.storyboard.tests.test_shot_list_jobs import proposal_for
from w_craft_back.personal_api.models import PersonalAPIPreference
from w_craft_back.personal_api.services import (
    create_connection, save_preference, update_connection,
)
from w_craft_back.services.image_generation.registry import MODEL_REGISTRY


MODEL = "gemini/gemini-2.5-flash"
FREE_MODEL = "openrouter/google/gemma-4-26b-a4b-it:free"
FIRST_KEY = "test-personal-text-first"
SECOND_KEY = "test-personal-text-second"


class ProviderRejection(Exception):
    def __init__(self, status_code):
        super().__init__("upstream request failed")
        self.status_code = status_code


@override_settings(
    PERSONAL_API_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    GEMINI_API_KEY="", OPENROUTER_API_KEY="platform-free-test-key",
    STORYBOARD_SHOT_LIST_MODEL=MODEL, STORYBOARD_SHOT_LIST_MODELS=MODEL,
    STORYBOARD_SHOT_METADATA_MODEL=FREE_MODEL,
    STORYBOARD_SHOT_METADATA_MODELS=FREE_MODEL,
    STORYBOARD_SHOT_LIST_THROTTLE_RATE="10000/min",
)
class StoryboardPersonalAPITests(TestCase):
    def setUp(self):
        self.user, self.token = make_user("personal-storyboard-owner")
        self.project = make_project(self.user, "Personal API film")
        self.scene = Scene.objects.create(
            project=self.project, title="Room", order=1,
            script_text="Anna enters the room.",
        )
        self.client = APIClient()
        self.client.credentials(HTTP_X_USER_TOKEN=self.token.key)
        self.scene_url = (
            f"/api/projects/{self.project.pk}/storyboard/scenes/{self.scene.pk}/"
        )
        self.completion = Mock(return_value=self.response(proposal_for(self.scene)))
        loader = patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=SimpleNamespace(completion=self.completion),
        )
        loader.start()
        self.addCleanup(loader.stop)

    @staticmethod
    def response(payload):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop",
        )])

    def connect(self, *, second=False, modality="text", model=MODEL):
        connection = create_connection(
            self.user, name="Backup" if second else "Main", provider="gemini",
            apiKey=SECOND_KEY if second else FIRST_KEY,
        )
        preference = PersonalAPIPreference.objects.filter(
            user=self.user, modality=modality,
        ).first()
        save_preference(
            self.user, modality, source="personal",
            routes=[*(preference.routes if preference else []), {
                "connectionId": connection["id"], "modelKey": model,
            }],
        )
        return connection

    def enqueue(self, source="personal", **extra):
        return self.client.post(f"{self.scene_url}shot-list-jobs/", {
            "requestId": str(uuid.uuid4()), "model": MODEL,
            "generationSource": source, "maxShots": 16, "language": "en",
            **extra,
        }, format="json")

    def test_paid_text_blocked_without_key_even_with_wallet_and_staff(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        CreditAccount.objects.create(user=self.user, available_balance=Decimal("100"))
        response = self.enqueue("platform")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["code"], "CRAFT_SUBSCRIPTION_REQUIRED")
        self.assertFalse(SceneStoryboardShotListJob.objects.exists())
        self.completion.assert_not_called()

    def test_image_connection_does_not_grant_paid_text(self):
        self.connect(modality="image", model="gemini-flash-image")
        response = self.enqueue()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["code"], "PERSONAL_API_NOT_CONFIGURED")
        self.completion.assert_not_called()

    def test_personal_job_executes_without_server_key_or_craft_wallet(self):
        self.connect()
        response = self.enqueue()
        self.assertEqual(response.status_code, 202, response.content)
        job = execute_shot_list_job(uuid.UUID(response.json()["jobId"]))
        self.assertEqual(job.status, "succeeded", job.error_code)
        self.assertEqual(self.completion.call_args.kwargs["api_key"], FIRST_KEY)
        self.assertNotIn(FIRST_KEY, json.dumps(job.request_snapshot))
        self.assertFalse(GenerationCharge.objects.exists())
        self.assertFalse(CreditAccount.objects.filter(user=self.user).exists())

    def test_queued_job_keeps_source_after_preferences_change(self):
        self.connect()
        response = self.enqueue()
        save_preference(self.user, "text", source="platform", routes=[])
        job = execute_shot_list_job(uuid.UUID(response.json()["jobId"]))
        self.assertEqual(job.status, "succeeded", job.error_code)
        self.assertEqual(self.completion.call_args.kwargs["api_key"], FIRST_KEY)

    def test_replaced_key_does_not_execute_old_queued_grant(self):
        connection = self.connect()
        response = self.enqueue()
        update_connection(self.user, connection["id"], apiKey=SECOND_KEY)
        job = execute_shot_list_job(uuid.UUID(response.json()["jobId"]))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "PERSONAL_API_CONNECTION_UNAVAILABLE")
        self.completion.assert_not_called()

    def test_definite_key_rejection_uses_next_personal_connection(self):
        self.connect()
        self.connect(second=True)
        self.completion.side_effect = [
            ProviderRejection(401), self.response(proposal_for(self.scene)),
        ]
        response = self.enqueue()
        job = execute_shot_list_job(uuid.UUID(response.json()["jobId"]))
        self.assertEqual(job.status, "succeeded", job.error_code)
        self.assertEqual(
            [call.kwargs["api_key"] for call in self.completion.call_args_list],
            [FIRST_KEY, SECOND_KEY],
        )
        self.assertFalse(GenerationCharge.objects.exists())

    def test_timeout_does_not_submit_to_backup(self):
        self.connect()
        self.connect(second=True)
        self.completion.side_effect = ProviderRejection(504)
        response = self.enqueue()
        job = execute_shot_list_job(uuid.UUID(response.json()["jobId"]))
        self.assertEqual(job.status, "failed")
        self.assertEqual(self.completion.call_count, 1)
        self.assertFalse(GenerationCharge.objects.exists())

    def test_personal_quote_is_not_blocked_by_frozen_empty_wallet(self):
        self.connect()
        CreditAccount.objects.create(user=self.user, is_frozen=True)
        response = self.client.post(reverse("credit-generation-estimate"), {
            "domain": "text", "generationSource": "personal",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["modelKey"], MODEL)
        self.assertEqual(payload["generationSource"], "personal")
        self.assertFalse(payload["isFree"])
        self.assertFalse(payload["accountFrozen"])
        self.assertTrue(payload["sufficientBalance"])
        self.assertEqual(payload["reservationAmount"], "0.00")
        self.completion.assert_not_called()

    def test_approval_pins_key_when_preferences_change_before_enqueue(self):
        self.connect()
        quote = self.client.post(reverse("credit-generation-estimate"), {
            "domain": "text", "generationSource": "personal",
        }, format="json").json()
        second = self.connect(second=True)
        save_preference(self.user, "text", source="personal", routes=[{
            "connectionId": second["id"], "modelKey": MODEL,
        }])
        response = self.enqueue(generationAccessToken=quote["generationAccessToken"])
        self.assertEqual(response.status_code, 202, response.content)
        job = execute_shot_list_job(uuid.UUID(response.json()["jobId"]))
        self.assertEqual(job.status, "succeeded", job.error_code)
        self.assertEqual(self.completion.call_args.kwargs["api_key"], FIRST_KEY)

    def test_approval_rejects_key_rotation_before_enqueue(self):
        first = self.connect()
        quote = self.client.post(reverse("credit-generation-estimate"), {
            "domain": "text", "generationSource": "personal",
        }, format="json").json()
        update_connection(self.user, first["id"], apiKey=SECOND_KEY)
        response = self.enqueue(generationAccessToken=quote["generationAccessToken"])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(SceneStoryboardShotListJob.objects.exists())
        self.completion.assert_not_called()

    def test_editor_frame_worker_uses_personal_key_without_wallet_charge(self):
        self.connect(modality="image", model="gemini-flash-image")
        payload = make_payload(self.scene)
        payload["shots"][0]["keyframes"][0]["generationReferences"] = []
        saved = save_editor_draft(
            actor=self.user, project_id=self.project.pk, scene_id=self.scene.pk,
            expected_revision=0, mutation_id=uuid.uuid4(), payload=payload,
        )
        spec = MODEL_REGISTRY["gemini-flash-image"]
        provider = SimpleNamespace(
            spec=spec, name=spec.key, model_id=spec.model_id, last_usage={},
            generate=Mock(return_value=[png()]),
        )
        with tempfile.TemporaryDirectory() as media, override_settings(
            MEDIA_ROOT=media,
        ):
            response = self.client.post(f"{self.scene_url}editor-frame-jobs/", {
                "shotId": "shot-local-1", "keyframeId": "keyframe-1",
                "expectedRevision": saved["revision"], "imageModel": spec.key,
                "requestId": str(uuid.uuid4()), "generationSource": "personal",
            }, format="json")
            self.assertEqual(response.status_code, 202, response.content)
            with patch(
                "w_craft_back.services.image_generation.personal.provider_from_spec",
                return_value=provider,
            ) as factory:
                job = execute_frame_job(uuid.UUID(response.json()["jobId"]))
            self.assertEqual(job.status, "succeeded", job.error_code)
            self.assertEqual(factory.call_args.kwargs["api_key"], FIRST_KEY)
            self.assertFalse(GenerationCharge.objects.exists())
            self.assertFalse(CreditAccount.objects.filter(user=self.user).exists())

    def test_free_metadata_works_with_personal_preference_but_no_text_key(self):
        save_preference(self.user, "text", source="personal", routes=[])
        self.completion.return_value = self.response({"value": "Anna enters"})
        response = self.client.post(f"{self.scene_url}suggest-shot-metadata/", {
            "field": "title", "range": {"start": 0, "end": 10},
            "sceneVersion": self.scene.version, "language": "en",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.completion.call_args.kwargs["model"], FREE_MODEL)
        self.assertNotIn("api_key", self.completion.call_args.kwargs)
        self.assertFalse(GenerationCharge.objects.exists())

    @override_settings(
        STORYBOARD_SHOT_METADATA_MODEL=MODEL, STORYBOARD_SHOT_METADATA_MODELS=MODEL,
    )
    def test_paid_override_cannot_turn_free_helper_into_paid_call(self):
        response = self.client.post(f"{self.scene_url}suggest-shot-metadata/", {
            "field": "title", "range": {"start": 0, "end": 10},
            "sceneVersion": self.scene.version,
        }, format="json")
        self.assertEqual(response.status_code, 503, response.content)
        self.completion.assert_not_called()
