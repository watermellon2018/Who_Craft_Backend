import os
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    CharacterGenerationJob,
    GenerationJobStatus,
    GenerationJobType,
)
from w_craft_back.character_studio.services.errors import CharacterStudioError
from w_craft_back.character_studio.services.generation_lifecycle import (
    CharacterProviderSelection,
    resolve_character_provider,
    retry_character_job,
)
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.character_studio.services.providers import (
    MockProvider,
    ProviderUserFacingError,
    RegistryCharacterProvider,
    get_image_provider,
)
from w_craft_back.character_studio.tests import CharacterStudioTestCase
from w_craft_back.profile.models import UserProfile
from w_craft_back.movie.project.serializers import (
    ProjectGenerationSettingsSerializer,
)
from w_craft_back.services.image_generation import (
    ImageProviderError,
    ModelSpec,
    serialize_model_spec,
)
from w_craft_back.services.image_generation.errors import (
    CODE_BAD_RESPONSE,
    CODE_UNAVAILABLE,
)


PROVIDER_FACTORY = (
    "w_craft_back.character_studio.services.generation_service."
    "get_image_provider"
)


class CharacterImageModelSelectionTests(CharacterStudioTestCase):
    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    def test_request_project_user_environment_and_default_precedence(self):
        character = self.create_character()
        profile, _ = UserProfile.objects.get_or_create(user=self.user_key.user)
        profile.image_generation_model = "imagen"
        profile.save(update_fields=["image_generation_model"])
        self.project.generation_settings = {"image_generation_model": "mock"}
        self.project.save(update_fields=["generation_settings"])

        selection = resolve_character_provider(
            project=self.project,
            actor=self.user_key,
            request_payload={"image_model": "google"},
            provider_operation="generate",
        )
        self.assertEqual((selection.key, selection.source), ("gemini", "request"))

        selection = resolve_character_provider(
            project=self.project,
            actor=self.user_key,
            request_payload={},
            provider_operation="generate",
        )
        self.assertEqual((selection.key, selection.source), ("mock", "project"))

        self.project.generation_settings = {"provider": "mock"}
        self.project.save(update_fields=["generation_settings"])
        selection = resolve_character_provider(
            project=self.project,
            actor=self.user_key,
            request_payload={},
            provider_operation="generate",
        )
        self.assertEqual((selection.key, selection.source), ("mock", "project"))

        self.project.generation_settings = {}
        self.project.save(update_fields=["generation_settings"])
        selection = resolve_character_provider(
            project=self.project,
            actor=self.user_key,
            request_payload={},
            provider_operation="generate",
        )
        self.assertEqual((selection.key, selection.source), ("gemini", "user"))

        profile.image_generation_model = ""
        profile.save(update_fields=["image_generation_model"])
        selection = resolve_character_provider(
            project=self.project,
            actor=self.user_key,
            request_payload={},
            provider_operation="generate",
        )
        self.assertEqual((selection.key, selection.source), ("mock", "env"))

        with patch.dict(os.environ, {"DEFAULT_IMAGE_MODEL": "mock"}):
            os.environ.pop("CHARACTER_STUDIO_IMAGE_PROVIDER", None)
            selection = resolve_character_provider(
                project=self.project,
                actor=self.user_key,
                request_payload={},
                provider_operation="generate",
            )
        self.assertEqual((selection.key, selection.source), ("mock", "default"))
        self.assertEqual(character.project_id, self.project.id)

    def test_unknown_and_unconfigured_override_create_no_job(self):
        character = self.create_character()
        service = CharacterGenerationService(execute_immediately=False)

        with self.assertRaises(CharacterStudioError) as unknown:
            service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {"variant_count": 1, "image_model": "not-a-model"},
            )
        self.assertEqual(unknown.exception.error_code, "IMAGE_MODEL_UNKNOWN")

        with patch.dict(os.environ, {}):
            os.environ.pop("GEMINI_API_KEY", None)
            with self.assertRaises(CharacterStudioError) as unconfigured:
                service.create_initial_variants(
                    self.user_key,
                    self.project.id,
                    character.character_id,
                    {
                        "variant_count": 1,
                        "image_model": "gemini-flash-image",
                    },
                )
        self.assertEqual(
            unconfigured.exception.error_code,
            "IMAGE_PROVIDER_NOT_CONFIGURED",
        )
        self.assertFalse(
            CharacterGenerationJob.objects.filter(character=character).exists()
        )

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    def test_reference_requires_reference_capability_before_enqueue(self):
        character = self.create_character()

        with self.assertRaises(CharacterStudioError) as error:
            resolve_character_provider(
                project=character.project,
                actor=self.user_key,
                request_payload={"image_model": "gemini-imagen-4"},
                provider_operation="reference",
            )

        self.assertEqual(
            error.exception.error_code,
            "MODEL_DOES_NOT_SUPPORT_IMAGE_INPUT",
        )
        self.assertFalse(
            CharacterGenerationJob.objects.filter(character=character).exists()
        )

    def test_multipart_create_from_reference_forwards_image_model(self):
        self.project.generation_settings = {
            "image_generation_model": "gemini-flash-image",
        }
        self.project.save(update_fields=["generation_settings"])
        client = APIClient()
        upload = SimpleUploadedFile(
            "reference.png",
            MockProvider._PLACEHOLDER_PNG,
            content_type="image/png",
        )

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                response = client.post(
                    f"/api/projects/{self.project.id}/characters/from-reference",
                    {
                        "name": "Reference actor",
                        "character_type": "human",
                        "variant_count": "1",
                        "image_model": "mock",
                        "reference_image": upload,
                    },
                    format="multipart",
                    HTTP_X_USER_TOKEN=str(self.user_key.key),
                    HTTP_IDEMPOTENCY_KEY="multipart-image-model",
                )

        self.assertEqual(response.status_code, 202, response.content)
        job = CharacterGenerationJob.objects.get(
            job_id=response.json()["generation_job"]["job_id"]
        )
        self.assertEqual(job.provider, "mock")
        self.assertEqual(job.provider_snapshot["source"], "request")
        self.assertEqual(job.request_payload["image_model"], "mock")

    def test_generation_preview_uses_request_image_model_override(self):
        character = self.create_character()
        self.project.generation_settings = {
            "image_generation_model": "gemini-flash-image",
        }
        self.project.save(update_fields=["generation_settings"])

        response = APIClient().get(
            (
                f"/api/projects/{self.project.id}/characters/"
                f"{character.character_id}/generation-preview"
            ),
            {"image_types": "portrait", "image_model": "mock"},
            HTTP_X_USER_TOKEN=str(self.user_key.key),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["provider"], "mock")
        self.assertEqual(response.json()["provider_source"], "request")
        self.assertEqual(
            response.json()["provider_snapshot"]["spec"]["key"],
            "mock",
        )

    def test_manual_reference_delegations_forward_image_model(self):
        character = self.create_character()
        reference = CharacterAsset.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            asset_type=CharacterAssetType.PORTRAIT,
            status=CharacterAssetStatus.READY,
            storage_path="tests/reference.png",
        )
        service = CharacterGenerationService(execute_immediately=False)
        generated_job = SimpleNamespace(job_id=uuid.uuid4())

        with patch.object(
            service,
            "generate_reference",
            return_value=generated_job,
        ) as generate_reference:
            service.generate_missing_references(
                self.user_key,
                self.project.id,
                character.character_id,
                {
                    "reference_types": ["full_body"],
                    "only_missing": False,
                    "image_model": "mock",
                },
            )
            service.correct_reference(
                self.user_key,
                self.project.id,
                character.character_id,
                reference.asset_id,
                {
                    "correction_prompt": "Keep the face, fix the lighting",
                    "image_model": "mock",
                },
            )

        missing_payload = generate_reference.call_args_list[0].args[3]
        correction_payload = generate_reference.call_args_list[1].args[3]
        self.assertEqual(missing_payload["image_model"], "mock")
        self.assertEqual(correction_payload["image_model"], "mock")

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    def test_image_model_changes_idempotency_hash(self):
        character = self.create_character()
        service = CharacterGenerationService(execute_immediately=False)
        first = service.create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {
                "variant_count": 1,
                "image_model": "mock",
                "_idempotency_key": "model-sensitive-request",
            },
        )

        with patch(
            "w_craft_back.character_studio.services.generation_lifecycle."
            "resolve_character_provider",
            side_effect=AssertionError("conflicting replay must not resolve"),
        ) as resolver:
            with self.assertRaises(CharacterStudioError):
                service.create_initial_variants(
                    self.user_key,
                    self.project.id,
                    character.character_id,
                    {
                        "variant_count": 1,
                        "image_model": "gemini-flash-image",
                        "_idempotency_key": "model-sensitive-request",
                    },
                )

        resolver.assert_not_called()
        self.assertEqual(first.provider, "mock")
        self.assertEqual(first.provider_snapshot["spec"]["key"], "mock")

    def test_idempotent_replay_does_not_require_dynamic_catalog(self):
        character = self.create_character()
        service = CharacterGenerationService(execute_immediately=False)
        spec = ModelSpec(
            key="openrouter-images:test/model",
            label="Catalog model",
            backend="openrouter-images",
            model_id="test/model",
            mode="image",
            supports_generate=True,
            supports_edit=False,
            supports_reference=False,
        )
        selection = CharacterProviderSelection(
            key=spec.key,
            source="request",
            snapshot={"source": "request", "spec": serialize_model_spec(spec)},
            model_name=spec.backend,
            model_version=spec.model_id,
        )
        params = {
            "variant_count": 1,
            "image_model": spec.key,
            "_idempotency_key": "dynamic-catalog-replay",
        }

        with patch(
            "w_craft_back.character_studio.services.generation_lifecycle."
            "resolve_character_provider",
            return_value=selection,
        ):
            first = service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                params,
            )

        with patch(
            "w_craft_back.character_studio.services.generation_lifecycle."
            "resolve_character_provider",
            side_effect=AssertionError("replay must not access the catalog"),
        ) as resolver:
            replay = service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                params,
            )

        resolver.assert_not_called()
        self.assertEqual(replay.job_id, first.job_id)

    def test_idempotent_replay_ignores_changed_catalog_snapshot_metadata(self):
        character = self.create_character()
        service = CharacterGenerationService(execute_immediately=False)
        first_spec = ModelSpec(
            key="openrouter-images:test/model",
            label="Catalog label v1",
            backend="openrouter-images",
            model_id="test/model",
            mode="image",
            supports_generate=True,
            supports_edit=False,
            supports_reference=False,
        )
        changed_spec = ModelSpec(
            key=first_spec.key,
            label="Catalog label v2",
            backend=first_spec.backend,
            model_id=first_spec.model_id,
            mode=first_spec.mode,
            supports_generate=True,
            supports_edit=False,
            supports_reference=False,
            description="Catalog metadata changed after enqueue.",
        )
        first_selection = CharacterProviderSelection(
            key=first_spec.key,
            source="request",
            snapshot={
                "source": "request",
                "spec": serialize_model_spec(first_spec),
            },
            model_name=first_spec.backend,
            model_version=first_spec.model_id,
        )
        changed_selection = CharacterProviderSelection(
            key=changed_spec.key,
            source="request",
            snapshot={
                "source": "request",
                "spec": serialize_model_spec(changed_spec),
            },
            model_name=changed_spec.backend,
            model_version=changed_spec.model_id,
        )
        params = {
            "variant_count": 1,
            "image_model": first_spec.key,
            "_idempotency_key": "dynamic-metadata-replay",
        }

        with patch(
            "w_craft_back.character_studio.services.generation_lifecycle."
            "resolve_character_provider",
            return_value=first_selection,
        ):
            first = service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                params,
            )

        with patch(
            "w_craft_back.character_studio.services.generation_lifecycle."
            "resolve_character_provider",
            return_value=changed_selection,
        ) as resolver:
            replay = service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                params,
            )

        resolver.assert_not_called()
        self.assertEqual(replay.job_id, first.job_id)
        self.assertEqual(replay.provider_snapshot, first_selection.snapshot)

    def test_project_settings_reject_non_generating_image_model(self):
        spec = ModelSpec(
            key="openrouter-images:test/svg-only",
            label="SVG-only model",
            backend="openrouter-images",
            model_id="test/svg-only",
            mode="svg",
            supports_generate=False,
            supports_edit=False,
            supports_reference=False,
        )
        serializer = ProjectGenerationSettingsSerializer(
            data={"image_generation_model": spec.key}
        )

        with patch(
            "w_craft_back.services.image_generation.resolve_model",
            return_value=spec,
        ):
            self.assertFalse(serializer.is_valid())

        self.assertIn("image_generation_model", serializer.errors)

    def test_retry_copies_provider_snapshot_without_resolution(self):
        character = self.create_character()
        original = CharacterGenerationService(
            execute_immediately=False
        ).create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {
                "variant_count": 1,
                "image_model": "mock",
                "quality": "high",
            },
        )
        original.status = GenerationJobStatus.FAILED
        original.error_code = "PROVIDER_UNAVAILABLE"
        original.save(update_fields=["status", "error_code", "updated_at"])

        with patch(
            "w_craft_back.character_studio.services.generation_lifecycle."
            "resolve_character_provider",
            side_effect=AssertionError("retry must not resolve a provider"),
        ):
            retried = retry_character_job(
                actor=self.user_key,
                job_id=original.job_id,
            )

        self.assertEqual(retried.provider, original.provider)
        self.assertEqual(retried.provider_snapshot, original.provider_snapshot)
        self.assertEqual(retried.model_name, original.model_name)
        self.assertEqual(retried.model_version, original.model_version)
        self.assertEqual(retried.request_payload, original.request_payload)

    def test_worker_dispatches_persisted_provider_snapshot(self):
        character = self.create_character()
        snapshot = {
            "source": "request",
            "spec": {
                "key": "openrouter-images:test/model",
                "backend": "openrouter-images",
                "model_id": "test/model",
            },
        }
        job = CharacterGenerationJob.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            actor=self.user_key,
            job_type=GenerationJobType.INITIAL_VARIANTS,
            status=GenerationJobStatus.QUEUED,
            variant_count=1,
            provider="openrouter-images:test/model",
            provider_snapshot=snapshot,
            request_payload={"image_type": "portrait"},
            compiled_prompt="portrait",
            compiled_metadata={"image_type": "portrait"},
        )

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                with patch(PROVIDER_FACTORY, return_value=MockProvider()) as factory:
                    completed = CharacterGenerationService().execute_queued_job(
                        job.job_id
                    )

        self.assertEqual(completed.status, GenerationJobStatus.COMPLETED)
        factory.assert_called_once_with(
            "openrouter-images:test/model",
            provider_snapshot=snapshot,
        )


class RegistryCharacterProviderParameterTests(TestCase):
    @staticmethod
    def _adapter(upstream):
        spec = ModelSpec(
            key="openrouter-images:test/model",
            label="Test model",
            backend="openrouter-images",
            model_id="test/model",
            mode="image",
            supports_generate=True,
            supports_edit=False,
            supports_reference=True,
        )
        adapter = RegistryCharacterProvider.__new__(RegistryCharacterProvider)
        adapter.spec = spec
        adapter.provider = upstream
        adapter.model_name = spec.backend
        adapter.model_version = spec.model_id
        adapter.logger = None
        return adapter

    @staticmethod
    def _job():
        return SimpleNamespace(
            job_id=uuid.uuid4(),
            character_id=uuid.uuid4(),
            timeout_seconds=120,
            provider_deadline=None,
            request_payload={},
        )

    @staticmethod
    def _compiled():
        return {
            "positive_prompt": "portrait",
            "metadata": {"image_type": "portrait"},
        }

    @patch.dict(
        os.environ,
        {"GEMINI_IMAGE_MODEL": "new-model-after-enqueue"},
    )
    def test_legacy_gemini_uses_snapshot_model_version(self):
        snapshot = {
            "source": "request",
            "spec": {
                "key": "gemini",
                "backend": "gemini-legacy",
                "model_id": "model-at-enqueue",
            },
        }

        provider = get_image_provider("gemini", snapshot)

        self.assertEqual(provider.model_version, "model-at-enqueue")

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
    def test_snapshot_dispatch_does_not_resolve_catalog_again(self):
        spec = ModelSpec(
            key="openrouter-images:test/model",
            label="Test model",
            backend="openrouter-images",
            model_id="test/model",
            mode="images",
            supports_generate=True,
            supports_edit=False,
            supports_reference=False,
            requires_env=("OPENROUTER_API_KEY",),
        )
        snapshot = {
            "source": "request",
            "spec": serialize_model_spec(spec),
        }
        unified_provider = SimpleNamespace()

        with patch(
            "w_craft_back.services.image_generation.resolve_model"
        ) as resolve_model:
            with patch(
                "w_craft_back.services.image_generation.provider_from_spec",
                return_value=unified_provider,
            ):
                provider = get_image_provider(spec.key, snapshot)

        resolve_model.assert_not_called()
        self.assertEqual(provider.spec, spec)
        self.assertIs(provider.provider, unified_provider)

    def test_generation_uses_capability_n_max_and_safe_parameter_whitelist(self):
        spec = ModelSpec(
            key="openrouter-images:test/model",
            label="Test model",
            backend="openrouter-images",
            model_id="test/model",
            mode="image",
            supports_generate=True,
            supports_edit=True,
            supports_reference=True,
            supported_parameters={
                "n": {"type": "integer", "min": 1, "max": 2},
                "aspect_ratio": {"type": "string"},
                "resolution": {"type": "string"},
                "size": {"type": "string"},
                "quality": {"type": "string"},
                "output_format": {"type": "string"},
                "background": {"type": "string"},
                "output_compression": {"type": "integer"},
                "seed": {"type": "integer"},
            },
        )

        class RecordingProvider:
            def __init__(self):
                self.calls = []

            def generate(self, prompt, **kwargs):
                self.calls.append(kwargs)
                return [MockProvider._PLACEHOLDER_PNG] * kwargs["variant_count"]

        unified_provider = RecordingProvider()
        provider = RegistryCharacterProvider.__new__(RegistryCharacterProvider)
        provider.spec = spec
        provider.provider = unified_provider
        provider.model_name = spec.backend
        provider.model_version = spec.model_id
        provider.logger = None
        job = SimpleNamespace(
            job_id=uuid.uuid4(),
            character_id=uuid.uuid4(),
            timeout_seconds=120,
            provider_deadline=None,
            request_payload={
                "aspect_ratio": "16:9",
                "resolution": "1K",
                "size": "1024x1024",
                "quality": "high",
                "output_format": "png",
                "background": "transparent",
                "output_compression": 80,
                "seed": "12",
                "provider": "must-not-pass",
                "extra_body": {"must": "not-pass"},
            },
        )
        compiled = {
            "positive_prompt": "portrait",
            "metadata": {"image_type": "portrait"},
        }

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                results = provider.generate_character_variants(
                    job,
                    compiled,
                    4,
                )

        self.assertEqual(len(results), 4)
        self.assertEqual(
            [call["variant_count"] for call in unified_provider.calls],
            [2, 2],
        )
        for call in unified_provider.calls:
            self.assertEqual(call["timeout"], 120)
            self.assertEqual(call["aspect_ratio"], "16:9")
            self.assertEqual(call["output_compression"], 80)
            self.assertNotIn("seed", call)
            self.assertNotIn("provider", call)
            self.assertNotIn("extra_body", call)

    def test_top_up_provider_error_after_partial_result_is_propagated(self):
        class FailingTopUpProvider:
            def __init__(self):
                self.calls = 0

            def _call(self):
                self.calls += 1
                if self.calls == 1:
                    return [MockProvider._PLACEHOLDER_PNG]
                raise ImageProviderError(
                    code=CODE_UNAVAILABLE,
                    message="Provider unavailable.",
                )

            def generate(self, prompt, **kwargs):
                return self._call()

            def generate_with_reference(self, prompt, image_bytes, **kwargs):
                return self._call()

        for operation in ("generate", "reference"):
            with self.subTest(operation=operation):
                upstream = FailingTopUpProvider()
                adapter = self._adapter(upstream)
                with patch.object(adapter, "_persist_variants") as persist:
                    with self.assertRaises(ProviderUserFacingError) as captured:
                        if operation == "generate":
                            adapter.generate_character_variants(
                                self._job(), self._compiled(), 2
                            )
                        else:
                            adapter.generate_from_reference(
                                self._job(),
                                self._compiled(),
                                b"reference",
                                "image/png",
                                2,
                            )
                self.assertEqual(captured.exception.error_code, CODE_UNAVAILABLE)
                persist.assert_not_called()

    def test_short_top_up_response_fails_before_persistence(self):
        class ShortTopUpProvider:
            def __init__(self):
                self.calls = 0

            def _call(self):
                self.calls += 1
                if self.calls == 1:
                    return [MockProvider._PLACEHOLDER_PNG]
                return []

            def generate(self, prompt, **kwargs):
                return self._call()

            def generate_with_reference(self, prompt, image_bytes, **kwargs):
                return self._call()

        for operation in ("generate", "reference"):
            with self.subTest(operation=operation):
                upstream = ShortTopUpProvider()
                adapter = self._adapter(upstream)
                with patch.object(adapter, "_persist_variants") as persist:
                    with self.assertRaises(ProviderUserFacingError) as captured:
                        if operation == "generate":
                            adapter.generate_character_variants(
                                self._job(), self._compiled(), 2
                            )
                        else:
                            adapter.generate_from_reference(
                                self._job(),
                                self._compiled(),
                                b"reference",
                                "image/png",
                                2,
                            )
                self.assertEqual(captured.exception.error_code, CODE_BAD_RESPONSE)
                persist.assert_not_called()
