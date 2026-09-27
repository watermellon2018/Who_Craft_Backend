"""Personal music credentials, payer isolation and safe candidate execution."""

import base64
import json
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from w_craft_back.credits.models import CreditAccount, GenerationCharge
from w_craft_back.movie.music.lifecycle import (
    enqueue_music_job, request_music_cancellation, retry_music_job,
)
from w_craft_back.movie.music.models import MusicGenerationJob, MusicJobStatus
from w_craft_back.movie.music.providers import MusicProviderError, ProviderSubmission
from w_craft_back.movie.music.providers.mock import MockAudioProvider
from w_craft_back.movie.music.providers.model_registry import public_audio_model_catalog
from w_craft_back.movie.music.worker import execute_music_job
from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.personal_api.access import (
    resolve_generation_access, sign_generation_access,
)
from w_craft_back.personal_api.services import (
    create_connection, revoke_connection, save_preference, update_connection,
)

from .helpers import instrumental_brief, make_project, make_user


@override_settings(
    PERSONAL_API_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"0" * 32).decode(),
    MUSIC_DEFAULT_AUDIO_MODEL="mock", MUSIC_ALLOW_MOCK=True,
    STABILITY_API_KEY="", GEMINI_API_KEY="", OPENROUTER_API_KEY="",
    MUSIC_STABILITY_COST_USD_PER_VARIANT="0",
)
class PersonalMusicTests(TestCase):
    def setUp(self):
        self.user = make_user(f"personal-music-{self._testMethodName}")
        self.project = make_project(self.user)
        self.account = CreditAccount.objects.create(user=self.user, is_frozen=True)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage = override_settings(MEDIA_ROOT=directory.name)
        storage.enable()
        self.addCleanup(storage.disable)
        self.first = self.connection("first-key-value")
        self.second = self.connection("second-key-value")
        self.preferences([self.first, self.second])

    def connection(self, key):
        return create_connection(
            self.user, name="Music account", provider="stability", apiKey=key,
        )["id"]

    def preferences(self, connections):
        save_preference(self.user, "music", source="personal", routes=[
            {"connectionId": connection, "modelKey": "stable-audio-3"}
            for connection in connections
        ])

    def enqueue(self, **kwargs):
        return enqueue_music_job(
            project=self.project, actor=self.user, brief=instrumental_brief(),
            variant_count=1, idempotency_key="personal-music", **kwargs,
        )[0]

    def assert_no_charge(self):
        self.assertFalse(GenerationCharge.objects.filter(account=self.account).exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance, 0)
        self.assertEqual(self.account.reserved_balance, 0)

    def test_personal_success_ignores_frozen_empty_wallet_server_key_and_price(self):
        job = self.enqueue()
        self.assertEqual(job.provider, "stability")
        self.assertEqual(
            job.provider_snapshot["generationAccess"]["source"], "personal",
        )
        self.assertNotIn("first-key-value", json.dumps(job.provider_snapshot))
        with patch("w_craft_back.movie.music.worker.get_music_provider",
                   return_value=MockAudioProvider()) as factory:
            completed = execute_music_job(job.pk)
        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        self.assertEqual(factory.call_args.kwargs["api_key"], "first-key-value")
        self.assert_no_charge()

    def test_paid_platform_requires_subscription_even_with_funds(self):
        self.account.available_balance = 100
        self.account.save()
        with self.assertRaises(PersonalAPIError) as raised:
            self.enqueue(model_key="stable-audio-3", generation_source="platform")
        self.assertEqual(raised.exception.code, "CRAFT_SUBSCRIPTION_REQUIRED")
        self.assertFalse(MusicGenerationJob.objects.exists())

    def test_explicit_free_mock_uses_no_wallet_or_personal_key(self):
        job = self.enqueue(model_key="mock", generation_source="platform")
        self.assertEqual(execute_music_job(job.pk).status, MusicJobStatus.COMPLETED)
        self.assert_no_charge()

    def test_disabled_mock_never_bypasses_access(self):
        with override_settings(MUSIC_ALLOW_MOCK=False), self.assertRaises(
            MusicProviderError
        ):
            self.enqueue(model_key="mock", generation_source="platform")
        self.assertFalse(MusicGenerationJob.objects.exists())

    def test_metadata_includes_personal_route_without_server_configuration(self):
        rows = public_audio_model_catalog(user=self.user)
        stable = next(row for row in rows if row["key"] == "stable-audio-3")
        self.assertTrue(stable["configured"])
        other = make_user("personal-music-other-catalog")
        rows = public_audio_model_catalog(user=other)
        stable = next(row for row in rows if row["key"] == "stable-audio-3")
        self.assertFalse(stable["configured"])

    def test_revoke_queued_key_uses_next_preapproved_connection(self):
        job = self.enqueue()
        revoke_connection(self.user, self.first)
        with patch("w_craft_back.movie.music.worker.get_music_provider",
                   return_value=MockAudioProvider()) as factory:
            completed = execute_music_job(job.pk)
        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        self.assertEqual(factory.call_args.kwargs["api_key"], "second-key-value")
        self.assertEqual(completed.provider_snapshot["generationCandidateIndex"], 1)
        self.assert_no_charge()

    def test_rotated_only_key_stops_queued_job_before_provider(self):
        self.preferences([self.first])
        job = self.enqueue()
        update_connection(self.user, self.first, apiKey="replacement-key-value")
        with patch("w_craft_back.movie.music.worker.get_music_provider") as factory:
            failed = execute_music_job(job.pk)
        factory.assert_not_called()
        self.assertEqual(failed.status, MusicJobStatus.FAILED)
        self.assertEqual(failed.error_code, "PERSONAL_API_CONNECTION_UNAVAILABLE")
        self.assert_no_charge()

    def test_retry_cannot_borrow_another_users_key(self):
        job = self.enqueue()
        request_music_cancellation(job)
        other = make_user("personal-music-other-retry")
        with self.assertRaises(PersonalAPIError):
            retry_music_job(job, actor=other)
        retried = retry_music_job(job, actor=self.user)
        self.assertEqual(
            retried.provider_snapshot["generationAccess"],
            job.provider_snapshot["generationAccess"],
        )
        self.assert_no_charge()

    def test_settings_changes_do_not_change_queued_routing(self):
        job = self.enqueue()
        self.preferences([self.second, self.first])
        with patch("w_craft_back.movie.music.worker.get_music_provider",
                   return_value=MockAudioProvider()) as factory:
            execute_music_job(job.pk)
        self.assertEqual(factory.call_args.kwargs["api_key"], "first-key-value")

    def test_confirmation_token_keeps_approved_routes_after_preferences_change(self):
        token = sign_generation_access(resolve_generation_access(self.user, "music"))
        save_preference(self.user, "music", source="platform", routes=[])
        job = self.enqueue(approval_token=token)
        with patch("w_craft_back.movie.music.worker.get_music_provider",
                   return_value=MockAudioProvider()) as factory:
            completed = execute_music_job(job.pk)
        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        self.assertEqual(factory.call_args.kwargs["api_key"], "first-key-value")
        self.assertNotIn(token, json.dumps(job.provider_snapshot))
        self.assert_no_charge()

    def test_definitive_rejection_can_use_next_candidate(self):
        class Rejected(MockAudioProvider):
            def submit(self, request, context):
                raise MusicProviderError("Rejected", code="MUSIC_PROVIDER_REJECTED",
                                         definitely_rejected=True, retryable=False)

        job = self.enqueue()
        with patch("w_craft_back.movie.music.worker.get_music_provider",
                   side_effect=[Rejected(), MockAudioProvider()]) as factory:
            completed = execute_music_job(job.pk)
        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        self.assertEqual(factory.call_count, 2)
        self.assert_no_charge()

    def test_unknown_outcome_never_calls_next_connection(self):
        class Unknown(MockAudioProvider):
            def submit(self, request, context):
                raise MusicProviderError(
                    "Unknown", code="MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                    outcome_unknown=True, retryable=False,
                )

        job = self.enqueue()
        with patch("w_craft_back.movie.music.worker.get_music_provider",
                   return_value=Unknown()) as factory:
            failed = execute_music_job(job.pk)
        self.assertEqual(failed.error_code, "MUSIC_PROVIDER_OUTCOME_UNKNOWN")
        self.assertEqual(factory.call_count, 1)
        self.assert_no_charge()

    def test_async_poll_remains_on_original_key_after_preference_change(self):
        class Async(MockAudioProvider):
            def submit(self, request, context):
                self.request = request
                return ProviderSubmission(
                    external_job_id="accepted", poll_after_seconds=1,
                )

            def poll(self, external_job_id, context, provider_metadata=None):
                return super().submit(self.request, context)

        job = self.enqueue()
        provider = Async()
        with patch("w_craft_back.movie.music.worker.get_music_provider",
                   return_value=provider) as factory:
            execute_music_job(job.pk)
            self.preferences([self.second])
            MusicGenerationJob.objects.filter(pk=job.pk).update(
                next_poll_at=timezone.now() - timedelta(seconds=1),
            )
            completed = execute_music_job(job.pk)
        self.assertEqual(completed.status, MusicJobStatus.COMPLETED)
        self.assertEqual([call.kwargs["api_key"] for call in factory.call_args_list],
                         ["first-key-value", "first-key-value"])
        self.assert_no_charge()

    def test_unexpected_error_after_submit_is_unknown_without_secret_log(self):
        class Broken(MockAudioProvider):
            def submit(self, request, context):
                raise ValueError("first-key-value")

        job = self.enqueue()
        with patch("w_craft_back.movie.music.worker.get_music_provider",
                   return_value=Broken()) as factory, patch(
            "w_craft_back.movie.music.worker.logger",
        ) as logger:
            failed = execute_music_job(job.pk)
        self.assertEqual(failed.error_code, "MUSIC_PROVIDER_OUTCOME_UNKNOWN")
        self.assertFalse(failed.error_retryable)
        self.assertEqual(factory.call_count, 1)
        logger.exception.assert_not_called()
        self.assertNotIn("first-key-value", str(logger.mock_calls))
        self.assert_no_charge()
