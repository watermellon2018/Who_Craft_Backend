"""Sound-effect personal payer and durable credential regression coverage."""

import base64
import json
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from w_craft_back.credits.models import CreditAccount, GenerationCharge
from w_craft_back.movie.sound_effects.errors import SoundEffectProviderError
from w_craft_back.movie.sound_effects.lifecycle import (
    cancel_sound_effect_job, enqueue_sound_effect_job, retry_sound_effect_job,
)
from w_craft_back.movie.sound_effects.models import (
    SoundEffectGenerationJob, SoundEffectJobStatus,
)
from w_craft_back.movie.sound_effects.providers.elevenlabs import GeneratedSoundEffect
from w_craft_back.movie.sound_effects.services import get_capabilities
from w_craft_back.movie.sound_effects.worker import execute_sound_effect_job
from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.personal_api.access import (
    resolve_generation_access, sign_generation_access,
)
from w_craft_back.personal_api.services import (
    create_connection, revoke_connection, save_preference,
)

from .helpers import make_project, make_user, mp3_bytes, request_payload


class FakeProvider:
    def generate(self, request, context):
        context.checkpoint()
        return GeneratedSoundEffect(
            payload=mp3_bytes(), mime_type="audio/mpeg", provider_request_id="accepted",
            provenance={"provider": "elevenlabs-sfx"},
        )


@override_settings(
    PERSONAL_API_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"1" * 32).decode(),
    ELEVENLABS_API_KEY="", SOUND_EFFECTS_ELEVENLABS_AUTO_COST_USD="",
    SOUND_EFFECTS_ELEVENLABS_COST_USD_PER_MINUTE="",
    SOUND_EFFECTS_ELEVENLABS_OUTPUT_FORMAT="mp3_44100_128",
)
class PersonalSoundEffectTests(TestCase):
    def setUp(self):
        self.user = make_user(f"personal-sfx-{self._testMethodName}")
        self.project = make_project(self.user)
        self.account = CreditAccount.objects.create(user=self.user, is_frozen=True)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage = override_settings(MEDIA_ROOT=directory.name)
        storage.enable()
        self.addCleanup(storage.disable)
        self.keys = [create_connection(
            self.user, name=f"SFX {index}", provider="elevenlabs",
            apiKey=f"sfx-key-{index}",
        )["id"] for index in range(2)]
        save_preference(self.user, "sound_effect", source="personal", routes=[
            {"connectionId": connection, "modelKey": "elevenlabs-sound-effects-v2"}
            for connection in self.keys
        ])

    def enqueue(self, **request):
        return enqueue_sound_effect_job(
            actor=self.user, project=self.project, request=request_payload(**request),
            idempotency_key="sfx-personal",
        )[0]

    def assert_no_charge(self):
        self.assertFalse(GenerationCharge.objects.filter(account=self.account).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance, 0)
        self.assertEqual(self.account.reserved_balance, 0)

    def test_personal_auto_duration_ignores_platform_price_key_and_balance(self):
        job = self.enqueue(durationSeconds=None)
        self.assertNotIn("sfx-key-", json.dumps(job.provider_snapshot))
        with patch("w_craft_back.movie.sound_effects.worker.get_sound_effect_provider",
                   return_value=FakeProvider()) as factory:
            completed = execute_sound_effect_job(job.pk)
        self.assertEqual(completed.status, SoundEffectJobStatus.COMPLETED)
        self.assertEqual(factory.call_args.kwargs["api_key"], "sfx-key-0")
        self.assert_no_charge()

    def test_platform_without_subscription_is_blocked(self):
        with self.assertRaises(PersonalAPIError) as raised:
            self.enqueue(generationSource="platform")
        self.assertEqual(raised.exception.code, "CRAFT_SUBSCRIPTION_REQUIRED")
        self.assertFalse(SoundEffectGenerationJob.objects.exists())

    def test_confirmation_token_pins_source_and_is_not_saved_in_request(self):
        token = sign_generation_access(
            resolve_generation_access(self.user, "sound_effect"),
        )
        save_preference(self.user, "sound_effect", source="platform", routes=[])
        job = self.enqueue(generationAccessToken=token)
        self.assertEqual(
            job.provider_snapshot["generationAccess"]["source"], "personal",
        )
        self.assertNotIn("generationAccessToken", job.request)
        self.assert_no_charge()

    def test_metadata_is_available_only_for_owner_with_personal_route(self):
        payload = get_capabilities(actor=self.user, project_id=self.project.pk)
        model = payload["models"][0]
        self.assertTrue(model["configured"])
        self.assertTrue(model["duration"]["autoSupported"])
        revoke_connection(self.user, self.keys[0])
        revoke_connection(self.user, self.keys[1])
        payload = get_capabilities(actor=self.user, project_id=self.project.pk)
        model = payload["models"][0]
        self.assertFalse(model["configured"])

    def test_revoked_queued_credentials_stop_before_provider(self):
        job = self.enqueue()
        for connection in self.keys:
            revoke_connection(self.user, connection)
        with patch(
            "w_craft_back.movie.sound_effects.worker.get_sound_effect_provider",
        ) as factory:
            failed = execute_sound_effect_job(job.pk)
        factory.assert_not_called()
        self.assertEqual(failed.status, SoundEffectJobStatus.FAILED)
        self.assertEqual(failed.error_code, "PERSONAL_API_CONNECTION_UNAVAILABLE")
        self.assert_no_charge()

    def test_retry_checks_owner_even_in_shared_project(self):
        job = self.enqueue()
        cancel_sound_effect_job(job)
        other = make_user("sfx-personal-other-retry")
        with self.assertRaises(PersonalAPIError):
            retry_sound_effect_job(job, actor=other)
        retried, replay = retry_sound_effect_job(job, actor=self.user)
        self.assertFalse(replay)
        self.assertEqual(retried.provider_snapshot, job.provider_snapshot)
        self.assert_no_charge()

    def test_confirmed_preaccept_rejection_can_use_next_key(self):
        class Rejected(FakeProvider):
            def generate(self, request, context):
                raise SoundEffectProviderError(
                    "Rejected", code="SOUND_EFFECT_PROVIDER_REJECTED",
                    definitely_rejected=True, retryable=False,
                )

        job = self.enqueue()
        with patch("w_craft_back.movie.sound_effects.worker.get_sound_effect_provider",
                   side_effect=[Rejected(), FakeProvider()]) as factory:
            completed = execute_sound_effect_job(job.pk)
        self.assertEqual(completed.status, SoundEffectJobStatus.COMPLETED)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(completed.provider_snapshot["generationCandidateIndex"], 1)
        self.assert_no_charge()

    def test_unknown_outcome_never_replays_on_another_key(self):
        class Unknown(FakeProvider):
            def generate(self, request, context):
                raise SoundEffectProviderError(
                    "Unknown", code="SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
                    outcome_unknown=True, retryable=False,
                )

        job = self.enqueue()
        with patch("w_craft_back.movie.sound_effects.worker.get_sound_effect_provider",
                   return_value=Unknown()) as factory:
            failed = execute_sound_effect_job(job.pk)
        self.assertEqual(failed.error_code, "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN")
        self.assertEqual(factory.call_count, 1)
        self.assert_no_charge()

    def test_unexpected_error_after_submit_does_not_offer_unsafe_retry(self):
        class Broken(FakeProvider):
            def generate(self, request, context):
                raise ValueError("sfx-key-0")

        job = self.enqueue()
        with patch("w_craft_back.movie.sound_effects.worker.get_sound_effect_provider",
                   return_value=Broken()) as factory, patch(
            "w_craft_back.movie.sound_effects.worker.logger",
        ) as logger:
            failed = execute_sound_effect_job(job.pk)
        self.assertEqual(failed.error_code, "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN")
        self.assertFalse(failed.error_retryable)
        self.assertEqual(factory.call_count, 1)
        logger.exception.assert_not_called()
        self.assertNotIn("sfx-key-0", str(logger.mock_calls))
        self.assert_no_charge()
