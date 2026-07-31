"""Focused regressions for JOB-01 durable local generation workers."""

from __future__ import annotations

import base64
from io import StringIO
import tempfile
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from w_craft_back.character_studio.models import (
    CharacterGenerationJob,
    GenerationJobStatus,
)
from w_craft_back.character_studio.services.generation_lifecycle import (
    claim_job,
    fail_job,
)
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.character_studio.services.providers import MockProvider
from w_craft_back.character_studio.tests import CharacterStudioTestCase
from w_craft_back.movie.poster.models import PosterGenerationJob, PosterJobStatus
from w_craft_back.movie.poster.services import _PLACEHOLDER_PNG_BASE64
from w_craft_back.movie.poster.test_p0_03_security import _project, _user_with_token


class CharacterWorkerContractTests(CharacterStudioTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_post_enqueues_without_provider_and_worker_once_completes(self):
        character = self.create_character()
        url = (
            f"/api/projects/{self.project.id}/characters/"
            f"{character.character_id}/generate-initial-variants"
        )
        provider_path = (
            "w_craft_back.character_studio.services.generation_service."
            "get_image_provider"
        )
        with patch(provider_path) as provider_factory:
            response = self.client.post(
                url,
                {"variant_count": 1},
                format="json",
                HTTP_X_USER_TOKEN=str(self.user_key.key),
                HTTP_IDEMPOTENCY_KEY="job01-character",
            )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["status"], GenerationJobStatus.QUEUED)
        provider_factory.assert_not_called()

        with patch(provider_path, return_value=MockProvider()):
            call_command("run_generation_worker", once=True, stdout=StringIO())
        polling = self.client.get(
            f"/api/generation-jobs/{response.json()['job_id']}",
            HTTP_X_USER_TOKEN=str(self.user_key.key),
        )
        self.assertEqual(polling.json()["status"], GenerationJobStatus.COMPLETED)

    def test_cancellation_request_fences_stale_terminal_update_and_retry_history(self):
        character = self.create_character()
        job = CharacterGenerationService(execute_immediately=False).create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {"variant_count": 1},
        )
        lease = claim_job(job.job_id)
        cancelled = self.client.post(
            f"/api/generation-jobs/{job.job_id}/cancellation-request",
            HTTP_X_USER_TOKEN=str(self.user_key.key),
        )
        self.assertEqual(cancelled.status_code, 202, cancelled.content)
        self.assertEqual(
            cancelled.json()["status"],
            GenerationJobStatus.CANCELLATION_REQUESTED,
        )
        fail_job(lease, error_code="STALE", error_message="must not win")
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJobStatus.CANCELLATION_REQUESTED)

        job.provider_started_at = None
        job.save(update_fields=["provider_started_at", "updated_at"])
        retried = self.client.post(
            f"/api/generation-jobs/{job.job_id}/retry",
            HTTP_X_USER_TOKEN=str(self.user_key.key),
        )
        self.assertEqual(retried.status_code, 202, retried.content)
        self.assertNotEqual(retried.json()["job_id"], str(job.job_id))
        replayed_retry = self.client.post(
            f"/api/generation-jobs/{job.job_id}/retry",
            HTTP_X_USER_TOKEN=str(self.user_key.key),
        )
        self.assertEqual(replayed_retry.status_code, 202, replayed_retry.content)
        self.assertEqual(replayed_retry.json()["job_id"], retried.json()["job_id"])
        CharacterGenerationJob.objects.filter(
            job_id=retried.json()["job_id"]
        ).update(status=GenerationJobStatus.FAILED)
        terminal_replay = self.client.post(
            f"/api/generation-jobs/{job.job_id}/retry",
            HTTP_X_USER_TOKEN=str(self.user_key.key),
        )
        self.assertEqual(terminal_replay.json()["job_id"], retried.json()["job_id"])
        history = self.client.get(
            f"/api/projects/{self.project.id}/characters/"
            f"{character.character_id}/generation-jobs",
            HTTP_X_USER_TOKEN=str(self.user_key.key),
        )
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual(len(history.json()["jobs"]), 2)


@override_settings(POSTER_GENERATION_USE_MOCK=True)
class PosterWorkerContractTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.client = APIClient()
        self.owner, self.token = _user_with_token("job01-poster-owner")
        self.project = _project(self.owner, "JOB-01 poster")

    def test_direct_upload_is_durable_and_worker_completes_queued_job(self):
        upload = SimpleUploadedFile(
            "reference.png",
            base64.b64decode(_PLACEHOLDER_PNG_BASE64),
            content_type="image/png",
        )
        provider_path = "w_craft_back.movie.poster.worker.resolve_provider_for_user"
        with patch(provider_path) as provider_factory:
            response = self.client.post(
                f"/api/projects/{self.project.id}/poster/generate/",
                {
                    "prompt": "A durable poster",
                    "style": "cinematic",
                    "format": "vertical",
                    "reference_image": upload,
                },
                format="multipart",
                HTTP_X_USER_TOKEN=self.token,
                HTTP_IDEMPOTENCY_KEY="job01-poster",
            )
        self.assertEqual(response.status_code, 202, response.content)
        provider_factory.assert_not_called()
        job = PosterGenerationJob.objects.get(pk=response.json()["jobId"])
        self.assertEqual(job.status, PosterJobStatus.QUEUED)
        self.assertTrue(job.reference_storage_key)
        self.assertTrue(default_storage.exists(job.reference_storage_key))

        call_command("run_generation_worker", once=True, stdout=StringIO())
        job.refresh_from_db()
        self.assertEqual(job.status, PosterJobStatus.COMPLETED)
        self.assertTrue(job.variants.exists())

    def test_poster_history_cancellation_and_retry_are_project_scoped(self):
        response = self.client.post(
            f"/api/projects/{self.project.id}/poster/generate/",
            {"prompt": "Queued", "style": "cinematic", "format": "vertical"},
            format="json",
            HTTP_X_USER_TOKEN=self.token,
            HTTP_IDEMPOTENCY_KEY="job01-poster-history",
        )
        job_id = response.json()["jobId"]
        self.assertEqual(response.json()["job_id"], job_id)
        cancelled = self.client.post(
            f"/api/projects/{self.project.id}/poster/jobs/{job_id}/cancellation-request/",
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(cancelled.status_code, 202, cancelled.content)
        self.assertEqual(cancelled.json()["status"], PosterJobStatus.CANCELLATION_REQUESTED)
        retried = self.client.post(
            f"/api/projects/{self.project.id}/poster/jobs/{job_id}/retry/",
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(retried.status_code, 202, retried.content)
        replayed_retry = self.client.post(
            f"/api/projects/{self.project.id}/poster/jobs/{job_id}/retry/",
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(replayed_retry.status_code, 202, replayed_retry.content)
        self.assertEqual(replayed_retry.json()["job_id"], retried.json()["job_id"])
        PosterGenerationJob.objects.filter(
            pk=retried.json()["job_id"]
        ).update(status=PosterJobStatus.FAILED)
        terminal_replay = self.client.post(
            f"/api/projects/{self.project.id}/poster/jobs/{job_id}/retry/",
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(terminal_replay.json()["job_id"], retried.json()["job_id"])

        history = self.client.get(
            f"/api/projects/{self.project.id}/poster/jobs/",
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual(len(history.json()["jobs"]), 2)
