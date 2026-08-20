from __future__ import annotations

import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from w_craft_back.credits.models import CreditAccount, GenerationCharge
from w_craft_back.movie.music.lifecycle import (
    MusicLifecycleError,
    claim_music_job,
    enqueue_music_job,
    fail_music_job,
    heartbeat_music_job,
    mark_music_provider_started,
    mark_music_provider_result_received,
    recover_stale_music_jobs,
    request_music_cancellation,
    retry_music_job,
)
from w_craft_back.movie.music.models import MusicGenerationJob, MusicJobStatus
from w_craft_back.movie.music.providers import (
    MusicProviderError,
    ProviderSubmission,
)
from w_craft_back.movie.music.providers.base import AudioProviderPricing
from w_craft_back.movie.music.providers.mock import MockAudioProvider
from w_craft_back.movie.music.worker import execute_music_job

from .helpers import instrumental_brief, make_project, make_user


class PaidMockAudioProvider(MockAudioProvider):
    name = "stability"
    model_name = "stable-audio-3"

    def capabilities(self):
        capabilities = super().capabilities()
        return capabilities.__class__(
            provider_name=self.name,
            provider_display_name="Stable Audio 3.0",
            model_name=self.model_name,
            content_modes=("instrumental",),
            variant_counts=(1,),
            supports_audio_reference=False,
        )

    def pricing(self, variant_count: int) -> AudioProviderPricing:
        return AudioProviderPricing(
            estimated_cost=Decimal("0.26") * variant_count,
            snapshot={
                "currency": "USD",
                "source": "stability-ai",
                "unitCostUsd": "0.26",
            },
        )


@override_settings(
    MUSIC_DEFAULT_AUDIO_MODEL="",
    MUSIC_GENERATION_PROVIDER="mock",
    MUSIC_ALLOW_MOCK=True,
)
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

    @override_settings(
        GEMINI_API_KEY="test-google-key",
        OPENROUTER_API_KEY="test-openrouter-key",
        STABILITY_API_KEY="test-stability-key",
    )
    def test_explicit_model_is_snapshotted_and_part_of_idempotency(self):
        CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("1"),
        )
        job, replay = enqueue_music_job(
            project=self.project,
            actor=self.owner,
            brief=instrumental_brief("Explicit Lyria"),
            variant_count=1,
            idempotency_key="music:explicit-model",
            model_key="lyria-3-pro",
        )

        self.assertFalse(replay)
        self.assertEqual(job.provider, "google-lyria")
        self.assertEqual(job.provider_snapshot["modelKey"], "lyria-3-pro")
        self.assertEqual(job.provider_snapshot["routeKey"], "google-gemini-direct")
        self.assertEqual(job.provider_snapshot["estimatedCostUsd"], "0.08")

        with self.assertRaises(MusicLifecycleError) as conflict:
            enqueue_music_job(
                project=self.project,
                actor=self.owner,
                brief=instrumental_brief("Explicit Lyria"),
                variant_count=1,
                idempotency_key="music:explicit-model",
                model_key="stable-audio-3",
            )
        self.assertEqual(conflict.exception.code, "MUSIC_IDEMPOTENCY_CONFLICT")

    @override_settings(ELEVENLABS_API_KEY="test-elevenlabs-key")
    def test_elevenlabs_enqueue_snapshots_duration_based_price(self):
        CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("1"),
        )
        brief = instrumental_brief("ElevenLabs cue")
        brief["durationSeconds"] = 30

        job, replay = enqueue_music_job(
            project=self.project,
            actor=self.owner,
            brief=brief,
            variant_count=1,
            idempotency_key="music:elevenlabs-price",
            model_key="elevenlabs-music-v2",
        )

        self.assertFalse(replay)
        self.assertEqual(job.provider, "elevenlabs-music-v2")
        self.assertEqual(job.provider_snapshot["estimatedCostUsd"], "0.075")
        self.assertEqual(
            job.provider_snapshot["pricing"]["durationSeconds"],
            30,
        )
        self.assertEqual(
            job.provider_snapshot["pricing"]["billingUnit"],
            "minute",
        )

    @override_settings(
        GEMINI_API_KEY="test-google-key",
        OPENROUTER_API_KEY="test-openrouter-key",
    )
    def test_worker_and_retry_keep_snapshot_when_defaults_drift(self):
        CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("1"),
        )
        original, _ = enqueue_music_job(
            project=self.project,
            actor=self.owner,
            brief=instrumental_brief("Snapshot route"),
            variant_count=1,
            idempotency_key="music:snapshot-route",
            model_key="lyria-3-pro",
        )
        original.status = MusicJobStatus.FAILED
        original.error_code = "MUSIC_PROVIDER_TIMEOUT"
        original.save(update_fields=("status", "error_code", "updated_at"))

        with override_settings(
            MUSIC_DEFAULT_AUDIO_MODEL="stable-audio-3",
            GEMINI_API_KEY="changed-but-present",
        ), patch(
            "w_craft_back.movie.music.lifecycle.get_music_provider",
            return_value=MockAudioProvider(),
        ):
            retry = retry_music_job(original, actor=self.owner)

        self.assertEqual(retry.provider_snapshot, original.provider_snapshot)
        self.assertEqual(retry.provider, "google-lyria")
        self.assertEqual(retry.model_name, "lyria-3-pro-preview")

        with patch(
            "w_craft_back.movie.music.worker.get_music_provider",
            return_value=MockAudioProvider(),
        ) as provider_factory:
            completed = execute_music_job(retry.pk)

        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        provider_factory.assert_called_once_with(
            "google-lyria",
            model_name="lyria-3-pro-preview",
        )

    def test_worker_supports_blank_legacy_provider_snapshot(self):
        job = self.enqueue(key="music:legacy-snapshot")
        MusicGenerationJob.objects.filter(pk=job.pk).update(provider_snapshot={})

        completed = execute_music_job(job.pk)

        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)

    def test_mock_worker_finalizes_two_verified_variants(self):
        job = self.enqueue()
        completed = execute_music_job(job.pk)
        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        self.assertEqual(completed.variants.count(), 2)
        self.assertTrue(
            all(variant.asset.file.name for variant in completed.variants.all())
        )

    def test_paid_provider_reserves_and_captures_fixed_price(self):
        provider = PaidMockAudioProvider()
        account = CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("1"),
        )
        with patch(
            "w_craft_back.movie.music.lifecycle.get_music_provider",
            return_value=provider,
        ):
            job, replay = enqueue_music_job(
                project=self.project,
                actor=self.owner,
                brief=instrumental_brief("Paid"),
                variant_count=1,
                idempotency_key="music:paid",
            )
        self.assertFalse(replay)
        account.refresh_from_db()
        self.assertEqual(account.available_balance, Decimal("0.74"))
        self.assertEqual(account.reserved_balance, Decimal("0.26"))

        with patch(
            "w_craft_back.movie.music.worker.get_music_provider",
            return_value=provider,
        ):
            completed = execute_music_job(job.pk)

        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        account.refresh_from_db()
        self.assertEqual(account.available_balance, Decimal("0.74"))
        self.assertEqual(account.reserved_balance, Decimal("0"))
        charge = GenerationCharge.objects.get(domain="music", job_id=str(job.pk))
        self.assertEqual(charge.actual_cost, Decimal("0.26"))
        self.assertEqual(charge.charged_amount, Decimal("0.26"))

    def test_async_paid_provider_polls_and_captures_exactly_once(self):
        class AsyncPaidProvider(PaidMockAudioProvider):
            submitted_request = None

            def submit(self, request, context):
                context.checkpoint()
                self.submitted_request = dict(request)
                return ProviderSubmission(
                    external_job_id="provider-generation-id",
                    poll_after_seconds=10,
                    provider_metadata={"pollCount": 0, "seed": 123},
                )

            def poll(self, external_job_id, context, provider_metadata=None):
                self.assertion = (
                    external_job_id,
                    dict(provider_metadata or {}),
                )
                return super().submit(self.submitted_request, context)

        provider = AsyncPaidProvider()
        account = CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("1"),
        )
        with patch(
            "w_craft_back.movie.music.lifecycle.get_music_provider",
            return_value=provider,
        ):
            job, _ = enqueue_music_job(
                project=self.project,
                actor=self.owner,
                brief=instrumental_brief("Async paid result"),
                variant_count=1,
                idempotency_key="music:paid-async",
            )
        with patch(
            "w_craft_back.movie.music.worker.get_music_provider",
            return_value=provider,
        ):
            processing = execute_music_job(job.pk)
            self.assertEqual(processing.status, MusicJobStatus.PROCESSING)
            self.assertEqual(processing.provider_job_id, "provider-generation-id")
            MusicGenerationJob.objects.filter(pk=job.pk).update(
                next_poll_at=timezone.now() - timedelta(seconds=1)
            )
            completed = execute_music_job(job.pk)

        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        self.assertEqual(provider.assertion[0], "provider-generation-id")
        self.assertEqual(provider.assertion[1]["pollCount"], 0)
        self.assertTrue(completed.provider_metadata["resultReceived"])
        account.refresh_from_db()
        charge = GenerationCharge.objects.get(domain="music", job_id=str(job.pk))
        self.assertEqual(account.available_balance, Decimal("0.74"))
        self.assertEqual(account.reserved_balance, Decimal("0"))
        self.assertEqual(charge.charged_amount, Decimal("0.26"))
        self.assertEqual(
            GenerationCharge.objects.filter(domain="music", job_id=str(job.pk)).count(),
            1,
        )

    def test_unknown_paid_outcome_captures_estimate_for_reconciliation(self):
        provider = PaidMockAudioProvider()
        account = CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("1"),
        )
        with patch(
            "w_craft_back.movie.music.lifecycle.get_music_provider",
            return_value=provider,
        ):
            job, _ = enqueue_music_job(
                project=self.project,
                actor=self.owner,
                brief=instrumental_brief("Unknown paid result"),
                variant_count=1,
                idempotency_key="music:paid-unknown",
            )
        claimed = claim_music_job(job.pk)
        mark_music_provider_started(claimed)

        fail_music_job(
            claimed,
            code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
            detail="Provider outcome is unknown.",
            http_status=502,
            retryable=False,
        )

        account.refresh_from_db()
        charge = GenerationCharge.objects.get(domain="music", job_id=str(job.pk))
        self.assertEqual(account.available_balance, Decimal("0.74"))
        self.assertEqual(account.reserved_balance, Decimal("0"))
        self.assertEqual(charge.charged_amount, Decimal("0.26"))
        self.assertTrue(charge.cost_is_estimate)
        self.assertEqual(
            charge.provider_usage["costSource"],
            "outcome-unknown-reservation",
        )

    def test_paid_result_read_failure_captures_provider_cost(self):
        class OversizedAsyncProvider(PaidMockAudioProvider):
            def submit(self, request, context):
                del request
                context.checkpoint()
                return ProviderSubmission(
                    external_job_id="oversized-generation-id",
                    poll_after_seconds=10,
                )

            def poll(self, external_job_id, context, provider_metadata=None):
                del external_job_id, provider_metadata
                context.checkpoint()
                raise MusicProviderError(
                    "Provider result is oversized.",
                    code="MUSIC_OUTPUT_TOO_LARGE",
                    http_status=502,
                    retryable=False,
                    cost_incurred=True,
                )

        provider = OversizedAsyncProvider()
        account = CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("1"),
        )
        with patch(
            "w_craft_back.movie.music.lifecycle.get_music_provider",
            return_value=provider,
        ):
            job, _ = enqueue_music_job(
                project=self.project,
                actor=self.owner,
                brief=instrumental_brief("Oversized paid result"),
                variant_count=1,
                idempotency_key="music:paid-oversized",
            )
        with patch(
            "w_craft_back.movie.music.worker.get_music_provider",
            return_value=provider,
        ):
            execute_music_job(job.pk)
            MusicGenerationJob.objects.filter(pk=job.pk).update(
                next_poll_at=timezone.now() - timedelta(seconds=1)
            )
            failed = execute_music_job(job.pk)

        self.assertEqual(failed.status, MusicJobStatus.FAILED)
        self.assertTrue(failed.provider_metadata["resultReceived"])
        account.refresh_from_db()
        charge = GenerationCharge.objects.get(domain="music", job_id=str(job.pk))
        self.assertEqual(account.available_balance, Decimal("0.74"))
        self.assertEqual(account.reserved_balance, Decimal("0"))
        self.assertEqual(charge.charged_amount, Decimal("0.26"))
        self.assertFalse(charge.cost_is_estimate)

    def test_confirmed_paid_result_is_charged_after_local_failure(self):
        provider = PaidMockAudioProvider()
        account = CreditAccount.objects.create(
            user=self.owner,
            available_balance=Decimal("1"),
        )
        with patch(
            "w_craft_back.movie.music.lifecycle.get_music_provider",
            return_value=provider,
        ):
            job, _ = enqueue_music_job(
                project=self.project,
                actor=self.owner,
                brief=instrumental_brief("Invalid local result"),
                variant_count=1,
                idempotency_key="music:paid-local-failure",
            )
        claimed = claim_music_job(job.pk)
        mark_music_provider_result_received(claimed)

        fail_music_job(
            claimed,
            code="MUSIC_OUTPUT_INVALID",
            detail="Provider returned invalid audio.",
            http_status=502,
            retryable=True,
        )

        account.refresh_from_db()
        charge = GenerationCharge.objects.get(domain="music", job_id=str(job.pk))
        self.assertEqual(account.available_balance, Decimal("0.74"))
        self.assertEqual(account.reserved_balance, Decimal("0"))
        self.assertEqual(charge.charged_amount, Decimal("0.26"))
        self.assertFalse(charge.cost_is_estimate)
        self.assertEqual(
            charge.provider_usage["costSource"],
            "confirmed-provider-result",
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

    def test_queued_cancel_is_immediately_terminal(self):
        job = self.enqueue()
        cancelled = request_music_cancellation(job)
        self.assertEqual(cancelled.status, MusicJobStatus.CANCELLED)

    def test_processing_job_cannot_be_cancelled(self):
        job = self.enqueue()
        claimed = claim_music_job(job.pk)

        with self.assertRaises(MusicLifecycleError) as raised:
            request_music_cancellation(job)

        self.assertEqual(raised.exception.code, "MUSIC_CANNOT_CANCEL")
        job.refresh_from_db()
        self.assertEqual(job.status, MusicJobStatus.PROCESSING)
        fail_music_job(
            claimed,
            code="TEST_CLEANUP",
            detail="cleanup",
            http_status=500,
            retryable=False,
        )

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

    def test_disabled_mock_blocks_snapshot_and_legacy_retries(self):
        for blank_snapshot in (False, True):
            with self.subTest(blank_snapshot=blank_snapshot):
                job = self.enqueue(
                    key=f"music:disabled-mock:{blank_snapshot}",
                    title=f"Disabled mock {blank_snapshot}",
                )
                job.status = MusicJobStatus.FAILED
                job.error_code = "MUSIC_PROVIDER_TIMEOUT"
                if blank_snapshot:
                    job.provider_snapshot = {}
                job.save(
                    update_fields=(
                        "status",
                        "error_code",
                        "provider_snapshot",
                        "updated_at",
                    )
                )

                with override_settings(MUSIC_ALLOW_MOCK=False):
                    with self.assertRaises(MusicProviderError) as raised:
                        retry_music_job(job, actor=self.owner)
                self.assertEqual(
                    raised.exception.code,
                    "MUSIC_MODEL_NOT_CONFIGURED",
                )

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
