from __future__ import annotations

import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from w_craft_back.movie.music.lifecycle import (
    MusicLifecycleError,
    claim_music_job,
    enqueue_music_job,
    fail_music_job,
    heartbeat_music_job,
    mark_music_provider_started,
    recover_stale_music_jobs,
    request_music_cancellation,
    retry_music_job,
)
from w_craft_back.movie.music.models import MusicGenerationJob, MusicJobStatus
from w_craft_back.movie.music.providers import MusicProviderError
from w_craft_back.movie.music.worker import execute_music_job

from .helpers import instrumental_brief, make_project, make_user


class MusicLifecycleTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.media = tempfile.TemporaryDirectory()
        cls.settings_override = override_settings(MEDIA_ROOT=cls.media.name)
        cls.settings_override.enable()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.settings_override.disable()
        cls.media.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        self.owner = make_user(f"lifecycle-{self._testMethodName}")
        self.project = make_project(self.owner)

    def enqueue(self, *, key="music:test", title="Night") -> MusicGenerationJob:
        job, replay = enqueue_music_job(
            project=self.project,
            actor=self.owner,
            brief=instrumental_brief(title),
            variant_count=2,
            idempotency_key=key,
        )
        self.assertFalse(replay)
        return job

    def test_idempotency_replays_same_intent_and_rejects_mismatch(self):
        first = self.enqueue()
        replay, was_replay = enqueue_music_job(
            project=self.project,
            actor=self.owner,
            brief=instrumental_brief("Night"),
            variant_count=2,
            idempotency_key="music:test",
        )
        self.assertTrue(was_replay)
        self.assertEqual(first.pk, replay.pk)
        with self.assertRaises(MusicLifecycleError) as raised:
            enqueue_music_job(
                project=self.project,
                actor=self.owner,
                brief=instrumental_brief("Different"),
                variant_count=2,
                idempotency_key="music:test",
            )
        self.assertEqual(raised.exception.code, "MUSIC_IDEMPOTENCY_CONFLICT")

    def test_mock_worker_finalizes_two_verified_variants(self):
        job = self.enqueue()
        completed = execute_music_job(job.pk)
        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        self.assertEqual(completed.variants.count(), 2)
        self.assertTrue(
            all(variant.asset.file.name for variant in completed.variants.all())
        )

    def test_worker_redacts_raw_provider_error_message(self):
        job = self.enqueue(key="music:redaction")

        class FailingProvider:
            def submit(self, request, context):
                del request, context
                raise MusicProviderError(
                    "upstream secret token and stack trace",
                    code="MUSIC_PROVIDER_TIMEOUT",
                    http_status=504,
                    retryable=True,
                )

        with patch(
            "w_craft_back.movie.music.worker.get_music_provider",
            return_value=FailingProvider(),
        ):
            failed = execute_music_job(job.pk)

        self.assertEqual(failed.status, MusicJobStatus.FAILED)
        self.assertEqual(failed.error_code, "MUSIC_PROVIDER_TIMEOUT")
        self.assertEqual(failed.error_detail, "Music provider timed out.")
        self.assertNotIn("secret", failed.error_detail)

    def test_queued_cancel_is_non_terminal_until_worker_confirms(self):
        job = self.enqueue()
        requested = request_music_cancellation(job)
        self.assertEqual(requested.status, MusicJobStatus.CANCELLATION_REQUESTED)
        cancelled = execute_music_job(job.pk)
        self.assertEqual(cancelled.status, MusicJobStatus.CANCELLED)

    def test_retry_creates_one_new_job_and_unknown_outcome_is_blocked(self):
        job = self.enqueue()
        claimed = claim_music_job(job.pk)
        self.assertIsNotNone(claimed)
        fail_music_job(
            claimed,
            code="MUSIC_PROVIDER_TIMEOUT",
            detail="Provider timed out.",
            http_status=504,
            retryable=True,
        )
        job.refresh_from_db()
        retry = retry_music_job(job, actor=self.owner)
        self.assertNotEqual(retry.pk, job.pk)
        self.assertEqual(retry.retry_of_id, job.pk)
        self.assertEqual(retry_music_job(job, actor=self.owner).pk, retry.pk)

        unknown = self.enqueue(key="music:unknown", title="Unknown")
        unknown.status = MusicJobStatus.FAILED
        unknown.error_code = "MUSIC_PROVIDER_OUTCOME_UNKNOWN"
        unknown.save()
        with self.assertRaises(MusicLifecycleError) as raised:
            retry_music_job(unknown, actor=self.owner)
        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_OUTCOME_UNKNOWN")

    def test_expired_started_lease_fails_and_old_fence_cannot_heartbeat(self):
        job = self.enqueue()
        claimed = claim_music_job(job.pk)
        mark_music_provider_started(claimed)
        MusicGenerationJob.objects.filter(pk=job.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        result = recover_stale_music_jobs(limit=10)
        self.assertIn(job.pk, result["failed"])
        self.assertFalse(heartbeat_music_job(job.pk, claimed.lease_token))
        job.refresh_from_db()
        self.assertEqual(job.error_code, "MUSIC_PROVIDER_OUTCOME_UNKNOWN")
