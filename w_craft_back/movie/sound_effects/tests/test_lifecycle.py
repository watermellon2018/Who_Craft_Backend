from decimal import Decimal
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from w_craft_back.credits.models import CreditAccount, GenerationCharge
from w_craft_back.movie.project.dashboard_models import Scene
from w_craft_back.movie.sound_effects.errors import (
    SoundEffectError,
    SoundEffectProviderError,
)
from w_craft_back.movie.sound_effects.lifecycle import (
    cancel_sound_effect_job,
    claim_sound_effect_job,
    enqueue_sound_effect_job,
    mark_sound_effect_provider_started,
    recover_stale_sound_effect_jobs,
    retry_sound_effect_job,
)
from w_craft_back.movie.sound_effects.models import (
    SceneSoundEffect,
    SoundEffect,
    SoundEffectJobStatus,
)
from w_craft_back.movie.sound_effects.providers.elevenlabs import (
    GeneratedSoundEffect,
)
from w_craft_back.movie.sound_effects.services import (
    apply_variant,
    enqueue_job as enqueue_job_payload,
    get_capabilities,
    retry_job as retry_job_payload,
)
from w_craft_back.movie.sound_effects.worker import execute_sound_effect_job

from .helpers import make_project, make_user, mp3_bytes, request_payload


class FakeProvider:
    name = "elevenlabs-sfx"
    model_name = "eleven_text_to_sound_v2"

    def generate(self, request, context):
        del request
        context.checkpoint()
        return GeneratedSoundEffect(
            payload=mp3_bytes(),
            mime_type="audio/mpeg",
            provider_request_id="req-worker",
            provenance={"provider": self.name, "model": self.model_name},
        )


@override_settings(
    ELEVENLABS_API_KEY="test-key",
    SOUND_EFFECTS_ELEVENLABS_COST_USD_PER_MINUTE="0.12",
    SOUND_EFFECTS_ELEVENLABS_AUTO_COST_USD="0.06",
    SOUND_EFFECTS_ELEVENLABS_OUTPUT_FORMAT="mp3_44100_128",
    SOUND_EFFECTS_JOB_LEASE_SECONDS=300,
)
class SoundEffectLifecycleTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media = tempfile.TemporaryDirectory()
        cls.media_override = override_settings(MEDIA_ROOT=cls.media.name)
        cls.media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.media_override.disable()
        cls.media.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.user = make_user(f"sfx-{self._testMethodName}")
        self.project = make_project(self.user)
        CreditAccount.objects.create(
            user=self.user,
            available_balance=Decimal("1"),
        )

    def enqueue(self, key="sfx:test", **overrides):
        return enqueue_sound_effect_job(
            project=self.project,
            actor=self.user,
            request=request_payload(**overrides),
            idempotency_key=key,
        )[0]

    def test_enqueue_snapshots_price_and_model_and_detects_conflict(self):
        job = self.enqueue()

        self.assertEqual(job.provider, "elevenlabs-sfx")
        self.assertEqual(job.provider_snapshot["version"], "sound-effect-provider-v1")
        self.assertEqual(job.provider_snapshot["estimatedCostUsd"], "0.005")
        charge = GenerationCharge.objects.get(
            domain="sound_effect",
            job_id=str(job.pk),
        )
        self.assertEqual(charge.reserved_amount, Decimal("0.005"))

        replay, was_replay = enqueue_sound_effect_job(
            project=self.project,
            actor=self.user,
            request=request_payload(),
            idempotency_key="sfx:test",
        )
        self.assertTrue(was_replay)
        self.assertEqual(replay.pk, job.pk)
        with self.assertRaises(SoundEffectError) as raised:
            enqueue_sound_effect_job(
                project=self.project,
                actor=self.user,
                request=request_payload(prompt="Different"),
                idempotency_key="sfx:test",
            )
        self.assertEqual(raised.exception.code, "SOUND_EFFECT_IDEMPOTENCY_CONFLICT")

    def test_idempotent_replay_survives_provider_configuration_drift(self):
        job = self.enqueue(key="sfx:config-drift")

        with patch(
            "w_craft_back.movie.sound_effects.lifecycle.get_sound_effect_provider",
            side_effect=SoundEffectError(
                "Provider is no longer configured.",
                code="SOUND_EFFECT_PROVIDER_NOT_CONFIGURED",
                http_status=503,
            ),
        ) as provider_factory:
            replay, was_replay = enqueue_sound_effect_job(
                project=self.project,
                actor=self.user,
                request=request_payload(),
                idempotency_key="sfx:config-drift",
            )

        self.assertTrue(was_replay)
        self.assertEqual(replay.pk, job.pk)
        provider_factory.assert_not_called()

    def test_capabilities_report_duration_loop_and_prompt_influence(self):
        payload = get_capabilities(actor=self.user, project_id=self.project.pk)
        model = payload["models"][0]

        self.assertEqual(payload["defaultModelKey"], "elevenlabs-sound-effects-v2")
        self.assertTrue(model["configured"])
        self.assertTrue(model["supportsLoop"])
        self.assertEqual(model["duration"]["minSeconds"], 0.5)
        self.assertEqual(model["duration"]["maxSeconds"], 30)
        self.assertTrue(model["duration"]["autoSupported"])
        self.assertEqual(model["promptInfluence"]["default"], 0.3)

        with override_settings(SOUND_EFFECTS_ELEVENLABS_AUTO_COST_USD=""):
            disabled = get_capabilities(
                actor=self.user,
                project_id=self.project.pk,
            )
        self.assertFalse(disabled["models"][0]["duration"]["autoSupported"])

    def test_worker_stores_one_variant_and_captures_reserved_cost(self):
        job = self.enqueue(key="sfx:worker")

        with patch(
            "w_craft_back.movie.sound_effects.worker.get_sound_effect_provider",
            return_value=FakeProvider(),
        ):
            completed = execute_sound_effect_job(job.pk)

        self.assertEqual(completed.status, SoundEffectJobStatus.COMPLETED)
        self.assertEqual(completed.variant.asset.mime_type, "audio/mpeg")
        replay = enqueue_job_payload(
            actor=self.user,
            project_id=self.project.pk,
            data=request_payload(),
            idempotency_key="sfx:worker",
        )
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["status"], SoundEffectJobStatus.COMPLETED)
        self.assertEqual(replay["stage"], "finalized")
        charge = GenerationCharge.objects.get(
            domain="sound_effect",
            job_id=str(job.pk),
        )
        self.assertEqual(charge.charged_amount, Decimal("0.005"))

    def test_cancel_releases_and_retry_copies_snapshot(self):
        job = self.enqueue(key="sfx:cancel")
        cancelled = cancel_sound_effect_job(job)
        self.assertEqual(cancelled.status, SoundEffectJobStatus.CANCELLED)

        with override_settings(SOUND_EFFECTS_ELEVENLABS_AUTO_COST_USD="0.99"):
            retry = retry_sound_effect_job(cancelled, actor=self.user)

        self.assertEqual(retry.provider_snapshot, cancelled.provider_snapshot)
        replay = retry_job_payload(
            actor=self.user,
            project_id=self.project.pk,
            job_id=cancelled.pk,
        )
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["jobId"], str(retry.pk))

    def test_apply_creates_immutable_version_and_scene_assignment(self):
        scene = Scene.objects.create(project=self.project, title="Hall", order=1)
        job, _ = enqueue_sound_effect_job(
            project=self.project,
            actor=self.user,
            request=request_payload(),
            idempotency_key="sfx:apply",
            target_scene=scene,
        )
        with patch(
            "w_craft_back.movie.sound_effects.worker.get_sound_effect_provider",
            return_value=FakeProvider(),
        ):
            completed = execute_sound_effect_job(job.pk)

        payload, created = apply_variant(
            actor=self.user,
            project_id=self.project.pk,
            job_id=job.pk,
            variant_id=completed.variant.pk,
            data={"targetEffectId": None, "title": "Door slam"},
            request=None,
        )

        self.assertTrue(created)
        effect = SoundEffect.objects.get(title="Door slam")
        self.assertEqual(effect.pk, payload["effectId"])
        self.assertEqual(str(effect.active_version_id), payload["id"])
        self.assertTrue(
            SceneSoundEffect.objects.filter(
                scene=scene,
                effect=effect,
                effect_version=effect.active_version,
            ).exists()
        )

    def test_ambiguous_paid_post_is_captured_and_not_retryable(self):
        class AmbiguousProvider(FakeProvider):
            def generate(self, request, context):
                del request
                context.checkpoint()
                raise SoundEffectProviderError(
                    "late timeout",
                    code="SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
                    http_status=502,
                    retryable=False,
                    outcome_unknown=True,
                    cost_incurred=True,
                )

        job = self.enqueue(key="sfx:ambiguous")
        with patch(
            "w_craft_back.movie.sound_effects.worker.get_sound_effect_provider",
            return_value=AmbiguousProvider(),
        ):
            failed = execute_sound_effect_job(job.pk)

        self.assertEqual(failed.status, SoundEffectJobStatus.FAILED)
        self.assertEqual(
            failed.error_code,
            "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
        )
        charge = GenerationCharge.objects.get(
            domain="sound_effect",
            job_id=str(job.pk),
        )
        self.assertEqual(charge.charged_amount, Decimal("0.005"))
        with self.assertRaises(SoundEffectError) as raised:
            retry_sound_effect_job(failed, actor=self.user)
        self.assertEqual(
            raised.exception.code,
            "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
        )

    def test_stale_started_provider_is_terminal_and_captured(self):
        queued = self.enqueue(key="sfx:stale-started")
        claimed = claim_sound_effect_job(queued.pk)
        self.assertIsNotNone(claimed)
        mark_sound_effect_provider_started(claimed)
        type(claimed).objects.filter(pk=claimed.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )

        recovered = recover_stale_sound_effect_jobs()

        claimed.refresh_from_db()
        self.assertIn(claimed.pk, recovered["failed"])
        self.assertEqual(claimed.status, SoundEffectJobStatus.FAILED)
        self.assertEqual(
            claimed.error_code,
            "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
        )
        charge = GenerationCharge.objects.get(
            domain="sound_effect",
            job_id=str(claimed.pk),
        )
        self.assertEqual(charge.charged_amount, Decimal("0.005"))
