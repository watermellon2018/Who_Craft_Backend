"""Personal image routing, credential isolation and durable payer boundaries."""

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import GenerationJobStatus
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.errors import CharacterStudioError
from w_craft_back.character_studio.services.generation_lifecycle import (
    retry_character_job,
)
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.character_studio.services.providers import MockProvider
from w_craft_back.credits.models import CreditAccount, GenerationCharge
from w_craft_back.movie.poster.errors import PosterError
from w_craft_back.movie.poster.generation_guard import provider_circuit_key
from w_craft_back.movie.poster.lifecycle import retry_poster_job
from w_craft_back.movie.poster.models import PosterJobStatus
from w_craft_back.movie.poster.services import enqueue_generation_job
from w_craft_back.movie.poster.worker import execute_poster_job
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.reference_library.errors import ReferenceConflict
from w_craft_back.movie.reference_library.lifecycle import retry_reference_job
from w_craft_back.movie.reference_library.models import (
    ProjectReference, ReferenceGenerationJob, ReferenceJobStatus,
)
from w_craft_back.movie.reference_library.providers import resolve_reference_provider
from w_craft_back.movie.reference_library.services import (
    enqueue_job as enqueue_reference,
)
from w_craft_back.movie.reference_library.worker import execute_reference_job
from w_craft_back.personal_api.models import PersonalAPIConnection
from w_craft_back.personal_api.access import sign_generation_access
from w_craft_back.personal_api.services import revoke_connection, save_preference
from w_craft_back.personal_api.tests.test_personal_api import (
    FIRST_SECRET, SECOND_SECRET, PersonalAPIBase,
)
from w_craft_back.services.image_generation.errors import (
    ImageProviderError, map_to_provider_error,
)
from w_craft_back.services.image_generation.litellm_provider import LiteLLMProvider
from w_craft_back.services.image_generation.openrouter_images import (
    OpenRouterImagesProvider,
)
from w_craft_back.services.image_generation.personal import (
    annotate_image_catalog, resolve_image_generation,
)
from w_craft_back.services.image_generation.registry import (
    MODEL_REGISTRY, model_catalog_row,
)
from w_craft_back.services.image_generation.resolver import provider_from_spec
from w_craft_back.services.image_generation.routing import provider_from_route_snapshot


FACTORY = "w_craft_back.services.image_generation.personal.provider_from_spec"


def fake_provider(spec=None, *, error=None):
    spec = spec or MODEL_REGISTRY["gemini-flash-image"]
    provider = Mock(spec=[])  # Avoid implicit Mock truthiness/usage attributes.
    provider.spec = spec
    provider.name = spec.key
    provider.model_id = spec.model_id
    provider.generate = Mock(
        return_value=[MockProvider._PLACEHOLDER_PNG], side_effect=error,
    )
    provider.usage_snapshot = lambda: {}
    return provider


class PersonalImageRouteTests(PersonalAPIBase):
    def setUp(self):
        super().setUp()
        self.first = self.connection()
        self.second = self.connection(secret=SECOND_SECRET)
        self.configure(self.first, self.second)

    def test_personal_route_does_not_require_server_key_or_leak_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = resolve_image_generation(self.alice)
        self.assertNotIn(FIRST_SECRET, repr(provider.provider_snapshot))
        self.assertEqual(provider.spec.key, "gemini-flash-image")
        with patch(FACTORY, return_value=fake_provider()) as factory:
            provider.generate("A portrait")
        self.assertEqual(factory.call_args.kwargs["api_key"], FIRST_SECRET)

    def test_authentication_rejection_tries_only_next_approved_connection(self):
        first = fake_provider(error=ImageProviderError(
            "IMAGE_PROVIDER_FORBIDDEN", "Key rejected", provider_status=401,
            rejected_before_acceptance=True,
        ))
        second = fake_provider()
        provider = resolve_image_generation(self.alice)
        with patch(FACTORY, side_effect=[first, second]) as factory:
            self.assertEqual(
                provider.generate("A portrait"), [MockProvider._PLACEHOLDER_PNG],
            )
        self.assertEqual(
            [call.kwargs["api_key"] for call in factory.call_args_list],
            [FIRST_SECRET, SECOND_SECRET],
        )
        self.assertFalse(
            PersonalAPIConnection.objects.get(pk=self.first["id"]).is_active,
        )
        self.assertTrue(
            PersonalAPIConnection.objects.get(pk=self.second["id"]).is_active,
        )

    def test_ambiguous_timeout_does_not_try_next_key_or_invalidate_current_key(self):
        first = fake_provider(error=ImageProviderError(
            "IMAGE_PROVIDER_UNAVAILABLE", "Timed out", http_status=504,
        ))
        provider = resolve_image_generation(self.alice)
        with patch(FACTORY, return_value=first) as factory:
            with self.assertRaises(ImageProviderError):
                provider.generate("A portrait")
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(
            PersonalAPIConnection.objects.get(pk=self.first["id"]).is_active,
        )

    def test_revoked_first_candidate_is_skipped_without_provider_call(self):
        provider = resolve_image_generation(self.alice)
        revoke_connection(self.alice, self.first["id"])
        with patch(FACTORY, return_value=fake_provider()) as factory:
            provider.generate("A portrait")
        self.assertEqual(factory.call_args.kwargs["api_key"], SECOND_SECRET)

    def test_snapshot_is_pinned_despite_preference_change(self):
        snapshot = resolve_image_generation(self.alice).provider_snapshot
        self.configure(self.second)
        provider = provider_from_route_snapshot(snapshot)
        with patch(FACTORY, return_value=fake_provider()) as factory:
            provider.generate("A portrait")
        self.assertEqual(factory.call_args.kwargs["api_key"], FIRST_SECRET)

    def test_all_revoked_credentials_fail_before_instantiating_provider(self):
        snapshot = resolve_image_generation(self.alice).provider_snapshot
        revoke_connection(self.alice, self.first["id"])
        revoke_connection(self.alice, self.second["id"])
        with patch(FACTORY) as factory:
            with self.assertRaises(ImageProviderError):
                provider_from_route_snapshot(snapshot)
        factory.assert_not_called()

    def test_platform_is_denied_even_with_server_key_and_positive_wallet(self):
        CreditAccount.objects.create(user=self.alice, available_balance=100)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-craft-key"}):
            with self.assertRaises(ImageProviderError) as caught:
                resolve_image_generation(self.alice, source="platform")
        self.assertEqual(caught.exception.code, "CRAFT_SUBSCRIPTION_REQUIRED")

    def test_personal_catalog_uses_routes_instead_of_server_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            rows = [model_catalog_row(spec) for spec in MODEL_REGISTRY.values()]
            personal = annotate_image_catalog(rows, self.alice, source="personal")
            platform = annotate_image_catalog(rows, self.alice, source="platform")
        self.assertEqual(
            [row["key"] for row in personal if row["configured"]],
            ["gemini-flash-image"],
        )
        self.assertFalse(any(row["configured"] for row in platform))

    def test_personal_circuit_does_not_share_platform_or_other_user_scope(self):
        provider = resolve_image_generation(self.alice)
        platform = fake_provider()
        self.assertNotEqual(
            provider_circuit_key(provider), provider_circuit_key(platform),
        )
        other = self.connection(self.bob)
        save_preference(self.bob, "image", source="personal", routes=[{
            "connectionId": other["id"], "modelKey": "gemini-flash-image",
        }])
        self.assertNotEqual(
            provider_circuit_key(provider),
            provider_circuit_key(resolve_image_generation(self.bob)),
        )

    def test_multireference_fallback_skips_model_that_cannot_accept_input_count(self):
        provider = resolve_image_generation(self.alice, operation="reference")
        with patch(FACTORY) as factory:
            with self.assertRaises(ImageProviderError) as caught:
                provider.generate_with_references("Prompt", [b"first", b"second"])
        self.assertEqual(caught.exception.code, "IMAGE_PROVIDER_CAPABILITY_MISMATCH")
        factory.assert_not_called()

    def test_generation_only_backup_does_not_hide_supported_reference_or_edit(self):
        save_preference(self.alice, "image", source="personal", routes=[
            {"connectionId": self.first["id"], "modelKey": "gemini-flash-image"},
            {"connectionId": self.second["id"], "modelKey": "gemini-imagen-4"},
        ])
        provider = resolve_image_generation(self.alice)
        self.assertTrue(provider.supports_reference())
        self.assertTrue(provider.supports_edit())
        reference_provider = resolve_reference_provider(
            actor=self.alice, project=SimpleNamespace(generation_settings={}),
            require_reference=True,
        )
        self.assertEqual(reference_provider.generation_access["operation"], "reference")
        self.assertEqual(
            [row["modelKey"] for row in reference_provider.generation_access[
                "candidates"
            ]],
            ["gemini-flash-image"],
        )


class PersonalImageAdapterTests(SimpleTestCase):
    def test_litellm_auth_rejection_retains_status_for_credential_invalidation(self):
        class AuthenticationError(Exception):
            pass

        sdk = SimpleNamespace(exceptions=SimpleNamespace(
            AuthenticationError=AuthenticationError,
        ))
        with patch.dict("sys.modules", {"litellm": sdk}):
            error = map_to_provider_error(AuthenticationError("Invalid key"))
        self.assertEqual(error.provider_status, 401)
        self.assertTrue(error.rejected_before_acceptance)

    def test_explicit_credentials_bypass_only_environment_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = provider_from_spec(
                MODEL_REGISTRY["gemini-flash-image"], api_key=FIRST_SECRET,
            )
        self.assertIsInstance(provider, LiteLLMProvider)

    def test_litellm_uses_instance_key_and_disables_transport_retries(self):
        completion = Mock(return_value={})
        sdk = SimpleNamespace(completion=completion)
        with patch.dict("sys.modules", {"litellm": sdk}), patch(
            "w_craft_back.services.image_generation.litellm_provider."
            "_extract_chat_images",
            return_value=[b"image"],
        ), patch.dict(os.environ, {"GEMINI_API_KEY": "fake-platform"}):
            for key in (FIRST_SECRET, SECOND_SECRET):
                LiteLLMProvider(
                    MODEL_REGISTRY["gemini-flash-image"], api_key=key,
                ).generate("A portrait")
            self.assertEqual(os.environ["GEMINI_API_KEY"], "fake-platform")
        self.assertEqual(
            [call.kwargs["api_key"] for call in completion.call_args_list],
            [FIRST_SECRET, SECOND_SECRET],
        )
        self.assertTrue(all(
            call.kwargs["num_retries"] == 0 for call in completion.call_args_list
        ))

    def test_personal_openrouter_503_does_not_automatically_retry_paid_request(self):
        response = Mock(status_code=503, text="temporary provider error")
        response.json.return_value = {"error": {"message": "temporary"}}
        session = Mock(headers={})
        session.post.return_value = response
        provider = OpenRouterImagesProvider(
            MODEL_REGISTRY["openrouter-flash-image"],
            session=session, api_key=FIRST_SECRET,
        )
        with self.assertRaises(ImageProviderError) as caught:
            provider.generate("A portrait")
        self.assertFalse(caught.exception.rejected_before_acceptance)
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.headers["Authorization"], f"Bearer {FIRST_SECRET}")
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_native_personal_key_is_not_forwarded_across_redirects(self):
        from w_craft_back.movie.poster.gemini_image import (
            GeminiImageError, edit_image_via_gemini, generate_image_via_gemini,
        )

        session = Mock()
        session.post.return_value.status_code = 307
        with patch(
            "w_craft_back.movie.poster.gemini_image.requests.Session",
            return_value=session,
        ):
            with self.assertRaises(GeminiImageError):
                generate_image_via_gemini("A portrait", api_key=FIRST_SECRET)
            with self.assertRaises(GeminiImageError):
                edit_image_via_gemini(b"image", "A change", api_key=FIRST_SECRET)
        self.assertEqual(session.post.call_count, 2)
        for call in session.post.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["headers"]["x-goog-api-key"], FIRST_SECRET)


@override_settings(POSTER_GENERATION_USE_MOCK=True, REFERENCE_IMAGE_PROVIDER="mock")
class PersonalImageDomainTests(PersonalAPIBase):
    def setUp(self):
        super().setUp()
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(MEDIA_ROOT=media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.project = Project.objects.create(
            owner=self.alice, title="Film", format="feature_film",
            annotation="", synopsis="",
        )
        self.actor = UserKey.objects.get(user=self.alice)
        self.connection_row = self.connection()
        self.configure(self.connection_row)
        self.account = CreditAccount.objects.create(
            user=self.alice, available_balance=0, is_frozen=True,
        )

    def poster_job(self, **options):
        return enqueue_generation_job(
            project=self.project, user=self.alice, prompt="A portrait",
            style="cinematic", format="vertical", generation_source="personal",
            **options,
        )[1]

    def character_job(self, **options):
        character = CharacterService().create_character(self.actor, self.project, {
            "name": "Mira", "age": 25, "gender": "woman",
            "short_description": "A filmmaker", "visual_style": "realism",
        })
        service = CharacterGenerationService(execute_immediately=False)
        return service.create_initial_variants(
            self.actor, self.project.pk, character.character_id,
            {"variant_count": 1, "generationSource": "personal", **options},
        )

    def reference_job(self, **options):
        reference = ProjectReference.objects.create(
            project=self.project, title="Medallion", category="prop",
            description="An ornate silver medallion on a plain background.",
            created_by=self.alice, updated_by=self.alice,
        )
        payload, _ = enqueue_reference(
            actor=self.alice, project_id=self.project.pk, reference_id=reference.pk,
            idempotency_key="personal-reference", data={
                "operation": "generate", "variantCount": 1,
                "expectedReferenceVersion": 1, "generationSource": "personal",
                **options,
            },
        )
        return ReferenceGenerationJob.objects.get(pk=payload["id"])

    def test_poster_personal_job_executes_without_wallet_or_mock_fallback(self):
        job = self.poster_job()
        self.assertEqual(
            job.provider_snapshot["generationAccess"]["source"], "personal",
        )
        with patch(FACTORY, return_value=fake_provider()) as factory:
            result = execute_poster_job(job.pk)
        self.assertEqual(result.status, PosterJobStatus.COMPLETED, result.error_message)
        self.assertEqual(factory.call_args.kwargs["api_key"], FIRST_SECRET)
        self.assertFalse(GenerationCharge.objects.exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance, 0)

    def test_character_personal_job_uses_actor_key_without_hidden_translation(self):
        job = self.character_job()
        with patch(FACTORY, return_value=fake_provider()) as factory, patch(
            "w_craft_back.character_studio.services.providers."
            "GeminiProvider._prepare_prompt",
        ) as translation:
            result = CharacterGenerationService().execute_queued_job(job.job_id)
        self.assertEqual(
            result.status, GenerationJobStatus.COMPLETED, result.error_message,
        )
        self.assertEqual(factory.call_args.kwargs["api_key"], FIRST_SECRET)
        translation.assert_not_called()
        self.assertFalse(GenerationCharge.objects.exists())

    def test_reference_personal_job_is_real_despite_mock_server_default(self):
        job = self.reference_job()
        self.assertNotEqual(job.provider, "mock")
        with patch(FACTORY, return_value=fake_provider()) as factory:
            result = execute_reference_job(job.pk)
        self.assertEqual(
            result.status, ReferenceJobStatus.COMPLETED, result.error_detail,
        )
        self.assertEqual(factory.call_args.kwargs["api_key"], FIRST_SECRET)
        self.assertFalse(GenerationCharge.objects.exists())

    def test_queued_poster_revocation_stops_before_network(self):
        job = self.poster_job()
        revoke_connection(self.alice, self.connection_row["id"])
        with patch(FACTORY) as factory:
            result = execute_poster_job(job.pk)
        self.assertEqual(result.status, PosterJobStatus.FAILED)
        self.assertEqual(result.error_code, "PERSONAL_API_CONNECTION_UNAVAILABLE")
        factory.assert_not_called()
        self.assertFalse(GenerationCharge.objects.exists())

    def test_poster_retry_cannot_borrow_another_actors_key(self):
        job = self.poster_job()
        job.status = PosterJobStatus.FAILED
        job.save(update_fields=["status"])
        with self.assertRaises(PosterError) as caught:
            retry_poster_job(job, actor=self.bob)
        self.assertEqual(caught.exception.code, "PERSONAL_API_CONNECTION_UNAVAILABLE")

    def test_reference_retry_cannot_borrow_another_actors_key(self):
        job = self.reference_job()
        job.status = ReferenceJobStatus.FAILED
        job.save(update_fields=["status"])
        with self.assertRaises(ReferenceConflict) as caught:
            retry_reference_job(job.pk, actor=self.bob)
        self.assertEqual(caught.exception.code, "PERSONAL_API_CONNECTION_UNAVAILABLE")

    def test_character_retry_rechecks_revoked_key(self):
        job = self.character_job()
        job.status = GenerationJobStatus.FAILED
        job.save(update_fields=["status"])
        revoke_connection(self.alice, self.connection_row["id"])
        with self.assertRaises(CharacterStudioError) as caught:
            retry_character_job(actor=self.actor, job_id=job.job_id)
        self.assertEqual(
            caught.exception.error_code, "PERSONAL_API_CONNECTION_UNAVAILABLE",
        )

    def test_explicit_paid_craft_selection_is_denied_even_with_mock_default(self):
        with self.assertRaises(PosterError) as caught:
            enqueue_generation_job(
                project=self.project, user=self.alice, prompt="A portrait",
                style="cinematic", format="vertical", generation_source="platform",
                requested_model="gemini-flash-image",
            )
        self.assertEqual(caught.exception.code, "CRAFT_SUBSCRIPTION_REQUIRED")

    def test_explicit_local_mock_ignores_frozen_wallet(self):
        _, job, _ = enqueue_generation_job(
            project=self.project, user=self.alice, prompt="A mock portrait",
            style="cinematic", format="vertical", generation_source="platform",
        )
        self.assertTrue(job.provider_snapshot["generationAccess"]["freePlatform"])
        self.assertFalse(GenerationCharge.objects.exists())

    def test_confirmation_pins_poster_connection_after_preferences_change(self):
        access = resolve_image_generation(self.alice).generation_access
        token = sign_generation_access(access)
        replacement = self.connection(secret=SECOND_SECRET)
        self.configure(replacement)
        job = self.poster_job(generation_access_token=token)
        self.assertEqual(job.provider_snapshot["generationAccess"], access)
        self.assertNotIn(token, repr(job.provider_snapshot))

    def test_poster_approval_remains_personal_after_default_changes_to_mock(self):
        access = resolve_image_generation(self.alice).generation_access
        token = sign_generation_access(access)
        self.configure(self.connection_row, source="platform")
        _, job, _ = enqueue_generation_job(
            project=self.project, user=self.alice, prompt="A portrait",
            style="cinematic", format="vertical", generation_access_token=token,
        )
        self.assertEqual(job.provider_snapshot["generationAccess"], access)
        self.assertFalse(job.provider_snapshot.get("mock"))
        with patch(FACTORY, return_value=fake_provider()) as factory:
            result = execute_poster_job(job.pk)
        self.assertEqual(result.status, PosterJobStatus.COMPLETED, result.error_message)
        self.assertEqual(factory.call_args.kwargs["api_key"], FIRST_SECRET)
        self.assertFalse(GenerationCharge.objects.exists())

    def test_character_consumes_approval_without_persisting_token(self):
        access = resolve_image_generation(self.alice).generation_access
        token = sign_generation_access(access)
        replacement = self.connection(secret=SECOND_SECRET)
        self.configure(replacement)
        job = self.character_job(generationAccessToken=token)
        self.assertEqual(job.provider_snapshot["generationAccess"], access)
        self.assertNotIn("generationAccessToken", job.request_payload)

    def test_reference_consumes_approval_without_persisting_token(self):
        access = resolve_image_generation(self.alice).generation_access
        token = sign_generation_access(access)
        replacement = self.connection(secret=SECOND_SECRET)
        self.configure(replacement)
        job = self.reference_job(generationAccessToken=token)
        self.assertEqual(job.provider_snapshot["generationAccess"], access)
        self.assertNotIn(token, repr(job.compiled_request))

    def test_mock_preference_cannot_grant_free_paid_automatic_route(self):
        from w_craft_back.character_studio.services.generation_lifecycle import (
            resolve_character_provider,
        )

        with self.assertRaises(CharacterStudioError) as caught:
            resolve_character_provider(
                project=self.project, actor=self.actor,
                request_payload={
                    "image_model": "mock", "routing_mode": "economy",
                    "generationSource": "platform",
                },
                provider_operation="generate",
            )
        self.assertEqual(caught.exception.error_code, "CRAFT_SUBSCRIPTION_REQUIRED")
