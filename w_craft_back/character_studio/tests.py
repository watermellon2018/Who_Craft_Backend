import base64
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.test import TestCase
from requests import HTTPError
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import CharacterAsset, CharacterAssetType, CharacterImage, CharacterOutfit
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.errors import IdentityLockedError, NotFoundError, SafetyRejectedError, ValidationError
from w_craft_back.character_studio.services.generation_service import CharacterGenerationService
from w_craft_back.character_studio.services.prompt_compiler import CharacterPromptCompiler
from w_craft_back.character_studio.services.providers import GeminiProvider, ProviderContentBlockedError
from w_craft_back.character_studio.services.revision_service import CharacterRevisionService
from w_craft_back.character_studio.services.safety import CharacterSafetyService
from w_craft_back.movie.project.models import Project

PROVIDER_SESSION = "w_craft_back.character_studio.services.providers.requests.Session"


class CharacterStudioTestCase(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="owner", password="x")
        self.user_key = UserKey.objects.create(user=user)
        self.project = Project.objects.create(
            user=self.user_key,
            title="Film",
            format="series",
            annot="Short",
            desc="Long",
        )
        self.service = CharacterService()
        self.previous_provider = os.environ.get("CHARACTER_STUDIO_IMAGE_PROVIDER")
        os.environ["CHARACTER_STUDIO_IMAGE_PROVIDER"] = "mock"

    def tearDown(self):
        if self.previous_provider is None:
            os.environ.pop("CHARACTER_STUDIO_IMAGE_PROVIDER", None)
        else:
            os.environ["CHARACTER_STUDIO_IMAGE_PROVIDER"] = self.previous_provider

    def create_character(self):
        return self.service.create_character(
            self.user_key,
            self.project,
            {
                "name": "Mira",
                "age": 17,
                "gender": "girl",
                "role": "lead",
                "short_description": "an anxious observant girl",
                "appearance_description": "green eyes copper hair slim sarcastic",
                "visual_style": "cinematic_realism",
            },
        )


class CharacterServiceTests(CharacterStudioTestCase):
    def test_create_character(self):
        character = self.create_character()
        self.assertEqual(character.name, "Mira")
        self.assertEqual(character.age, 17)
        self.assertEqual(character.project, self.project)
        self.assertIsNotNone(character.active_appearance)
        self.assertEqual(character.revisions.count(), 1)

    def test_invalid_age_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            self.service.create_character(
                self.user_key,
                self.project,
                {"name": "Mira", "age": "not-a-number"},
            )

    def test_update_character(self):
        character = self.create_character()
        updated = self.service.update_character(
            self.user_key,
            self.project.id,
            character.character_id,
            {"role": "protagonist", "speech_style": "dry"},
        )
        self.assertEqual(updated.role, "protagonist")
        self.assertEqual(updated.revisions.count(), 2)

    def test_delete_character_removes_record(self):
        character = self.create_character()
        self.service.delete_character(self.user_key, self.project.id, character.character_id)
        with self.assertRaises(NotFoundError):
            self.service.get_character(self.user_key, self.project.id, character.character_id)

    def test_lock_identity(self):
        character = self.create_character()
        locked = self.service.lock_identity(
            self.user_key,
            self.project.id,
            character.character_id,
            {"appearance_id": str(character.active_appearance_id), "confirm": True},
        )
        self.assertTrue(locked.identity_locked)
        self.assertEqual(locked.revisions.count(), 2)

    def test_create_outfit_and_single_default(self):
        character = self.create_character()
        first = CharacterOutfit.objects.create(character=character, name="School", is_default=True)
        second = CharacterOutfit.objects.create(character=character, name="Street")
        from w_craft_back.character_studio.repositories.repositories import OutfitRepository
        OutfitRepository().set_default(character, second)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)

    def test_database_rejects_duplicate_primary_assets(self):
        character = self.create_character()
        CharacterAsset.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            asset_type=CharacterAssetType.PORTRAIT,
            is_primary=True,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            CharacterAsset.objects.create(
                character=character,
                project=self.project,
                user=self.user_key,
                asset_type=CharacterAssetType.FULL_BODY,
                is_primary=True,
            )


class PromptCompilerTests(CharacterStudioTestCase):
    def test_initial_prompt(self):
        character = self.create_character()
        result = CharacterPromptCompiler().compile(
            character=character,
            appearance=character.active_appearance,
            outfit=None,
            region="full_character",
            controls={},
        )
        self.assertIn("17-year-old", result["positive_prompt"])
        self.assertIn("different person", result["negative_prompt"])

    def test_hair_edit_prompt(self):
        character = self.create_character()
        result = CharacterPromptCompiler().compile(
            character=character,
            appearance=character.active_appearance,
            outfit=None,
            region="hair",
            controls={"hair_color": "black"},
            preserve={"face": True},
        )
        self.assertIn("Modify only hair", result["edit_instruction"])
        self.assertIn("keep face", result["edit_instruction"])

    def test_outfit_edit_prompt(self):
        character = self.create_character()
        result = CharacterPromptCompiler().compile(
            character=character,
            appearance=character.active_appearance,
            outfit=None,
            region="outfit",
            controls={"outfit_preset": "school_uniform"},
            preserve={"identity": True},
        )
        self.assertIn("Modify only outfit", result["edit_instruction"])
        self.assertIn("keep face", result["edit_instruction"])

    def test_identity_lock_forces_preserve_identity(self):
        character = self.create_character()
        result = CharacterPromptCompiler().compile(
            character=character,
            appearance=character.active_appearance,
            outfit=None,
            region="hair",
            controls={},
            preserve={"identity": False},
            identity_locked=True,
        )
        self.assertTrue(result["metadata"]["preserve"]["identity"])


class RevisionTests(CharacterStudioTestCase):
    def test_create_and_restore_revision(self):
        character = self.create_character()
        revision_service = CharacterRevisionService()
        revision = revision_service.create_revision(character, "manual_update", change_summary="checkpoint")
        restored = revision_service.restore_revision(character, revision)
        self.assertEqual(restored.change_type, "restore_revision")
        self.assertEqual(character.revisions.count(), 3)


class GenerationFlowTests(CharacterStudioTestCase):
    def test_full_generation_apply_lock_edit_restore_flow(self):
        character = self.create_character()
        generation = CharacterGenerationService()
        initial_job = generation.create_initial_variants(self.user_key, self.project.id, character.character_id, {"variant_count": 4})
        self.assertEqual(initial_job.status, "completed")
        self.assertEqual(initial_job.variants.count(), 4)

        variant = initial_job.variants.first()
        CharacterService().apply_variant(self.user_key, self.project.id, character.character_id, variant.variant_id, {"apply_as": "current_reference"})
        character.refresh_from_db()
        self.assertIsNotNone(character.current_revision)

        CharacterService().lock_identity(
            self.user_key,
            self.project.id,
            character.character_id,
            {"reference_image_id": str(character.canonical_reference_image_id), "confirm": True},
        )
        hair_job = generation.generate_edit_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {
                "region": "hair",
                "controls": {"hair_color": "black"},
                "text_refinement": "windblown",
                "preserve": {"identity": True, "face": True},
                "variant_count": 4,
            },
        )
        hair_variant = hair_job.variants.first()
        CharacterService().apply_variant(self.user_key, self.project.id, character.character_id, hair_variant.variant_id, {"apply_as": "current_reference"})
        previous = character.revisions.order_by("revision_number").first()
        restored = CharacterRevisionService().restore_revision(character, previous)
        self.assertEqual(restored.change_type, "restore_revision")

    def test_generation_validation(self):
        character = self.create_character()
        generation = CharacterGenerationService()
        with self.assertRaises(ValidationError):
            generation.create_initial_variants(self.user_key, self.project.id, character.character_id, {"variant_count": 3})
        with self.assertRaises(ValidationError):
            generation.generate_edit_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {"region": "hair", "text_refinement": "x" * 501, "variant_count": 4},
            )
        CharacterService().lock_identity(self.user_key, self.project.id, character.character_id, {"confirm": True})
        with self.assertRaises(IdentityLockedError):
            generation.generate_edit_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {"region": "face", "controls": {"face_shape": "square"}, "variant_count": 4},
            )
        with self.assertRaises(SafetyRejectedError):
            CharacterSafetyService().validate_user_text("nsfw underage character")

    def test_variant_count_must_be_numeric(self):
        character = self.create_character()
        with self.assertRaises(ValidationError):
            CharacterGenerationService().create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {"variant_count": "many"},
            )

    def test_generation_accepts_create_page_variant_counts(self):
        character = self.create_character()
        generation = CharacterGenerationService()

        for variant_count in (1, 2, 4):
            job = generation.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {"variant_count": variant_count},
            )
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.variants.count(), variant_count)

    def test_apply_current_reference_keeps_single_primary_and_canonical_asset(self):
        character = self.create_character()
        job = CharacterGenerationService().create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {"variant_count": 2},
        )
        variants = list(job.variants.order_by("variant_index"))

        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            character.character_id,
            variants[0].variant_id,
            {"apply_as": "current_reference"},
        )
        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            character.character_id,
            variants[1].variant_id,
            {"apply_as": "current_reference"},
        )

        self.assertEqual(character.assets.filter(is_primary=True).count(), 1)
        self.assertEqual(character.assets.filter(is_canonical=True).count(), 1)
        self.assertTrue(character.assets.get(asset_id=variants[1].asset_id).is_primary)
        self.assertTrue(character.assets.get(asset_id=variants[1].asset_id).is_canonical)

    def test_generation_saves_active_images_by_type(self):
        character = self.create_character()
        generation = CharacterGenerationService()

        portrait_job = generation.create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {"variant_count": 2, "image_type": "portrait"},
        )
        full_body_job = generation.create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {"variant_count": 2, "image_type": "full_body"},
        )

        portrait_image = CharacterImage.objects.get(character=character, image_type="portrait", is_active=True)
        full_body_image = CharacterImage.objects.get(character=character, image_type="full_body", is_active=True)
        self.assertEqual(portrait_image.asset.source_job_id, portrait_job.job_id)
        self.assertEqual(full_body_image.asset.source_job_id, full_body_job.job_id)
        self.assertNotEqual(portrait_image.image_url, full_body_image.image_url)

        generation.generate_edit_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {
                "region": "face",
                "image_type": "portrait",
                "controls": {"age": 35, "changed_fields": ["age"]},
                "previous_values": {"age": 17},
                "new_values": {"age": 35},
                "variant_count": 2,
            },
        )

        self.assertEqual(CharacterImage.objects.filter(character=character, image_type="portrait", is_active=True).count(), 1)
        full_body_image.refresh_from_db()
        self.assertTrue(full_body_image.is_active)

    def test_initial_image_set_generates_all_editor_modes(self):
        character = self.create_character()

        jobs = CharacterGenerationService().create_initial_image_set(
            self.user_key,
            self.project.id,
            character.character_id,
            {"variant_count": 2},
        )

        self.assertEqual(len(jobs), 4)
        self.assertEqual([job.request_payload["image_type"] for job in jobs], ["portrait", "full_body", "scene", "reference_sheet"])
        self.assertEqual(
            set(CharacterImage.objects.filter(character=character, is_active=True).values_list("image_type", flat=True)),
            {"portrait", "full_body", "scene", "reference_sheet"},
        )


class GeminiProviderTests(TestCase):
    def test_predict_request_uses_documented_imagen_payload(self):
        image_bytes = base64.b64encode(b"png-bytes").decode("ascii")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "predictions": [
                {"bytesBase64Encoded": image_bytes, "width": 768, "height": 1024},
            ],
        }
        session = Mock()
        session.post.return_value = response

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"):
                with patch.dict(
                    os.environ,
                    {
                        "GEMINI_API_KEY": "test-key",
                        "GEMINI_IMAGE_MODEL": "imagen-4.0-generate-001",
                        "GEMINI_SEND_NEGATIVE_PROMPT": "",
                    },
                    clear=False,
                ):
                    with patch(PROVIDER_SESSION, return_value=session):
                        provider = GeminiProvider()
                        variants = provider.generate_character_variants(
                            SimpleNamespace(job_id=uuid4()),
                            {
                                "positive_prompt": (
                                    "Create a clean character design of a wizard"
                                ),
                                "negative_prompt": "extra limbs",
                            },
                            4,
                        )

        url = session.post.call_args.args[0]
        kwargs = session.post.call_args.kwargs
        parameters = kwargs["json"]["parameters"]

        self.assertNotIn("key=", url)
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-key")
        self.assertEqual(parameters["sampleCount"], 4)
        self.assertEqual(parameters["aspectRatio"], "3:4")
        self.assertEqual(parameters["personGeneration"], "allow_adult")
        self.assertNotIn("negativePrompt", parameters)
        self.assertTrue(
            variants[0]["storage_path"].startswith("character-studio/jobs/")
        )
        self.assertEqual(
            variants[0]["image_url"],
            f"/media/{variants[0]['storage_path']}",
        )

    def test_http_error_includes_google_response_without_api_key(self):
        response = Mock()
        response.status_code = 400
        response.reason = "Bad Request"
        response.raise_for_status.side_effect = HTTPError("400 Client Error")
        response.json.return_value = {
            "error": {"message": "Unknown name negativePrompt"}
        }
        session = Mock()
        session.post.return_value = response

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False):
            with patch(PROVIDER_SESSION, return_value=session):
                with self.assertRaises(RuntimeError) as ctx:
                    GeminiProvider().generate_character_variants(
                        SimpleNamespace(job_id=uuid4()),
                        {
                            "positive_prompt": "Create a character",
                            "negative_prompt": "",
                        },
                        4,
                    )

        self.assertIn("Unknown name negativePrompt", str(ctx.exception))
        self.assertNotIn("test-key", str(ctx.exception))

    def test_non_ascii_prompt_is_translated_before_imagen_request(self):
        translate_response = Mock()
        translate_response.raise_for_status.return_value = None
        translate_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": (
                                    "Create a clean character design "
                                    "of a detective"
                                )
                            }
                        ]
                    }
                }
            ]
        }
        image_response = Mock()
        image_response.raise_for_status.return_value = None
        image_response.json.return_value = {
            "predictions": [
                {"bytesBase64Encoded": base64.b64encode(b"png-bytes").decode("ascii")},
            ],
        }
        session = Mock()
        session.post.side_effect = [translate_response, image_response]

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"):
                with patch.dict(
                    os.environ,
                    {"GEMINI_API_KEY": "test-key"},
                    clear=False,
                ):
                    with patch(PROVIDER_SESSION, return_value=session):
                        GeminiProvider().generate_character_variants(
                            SimpleNamespace(job_id=uuid4()),
                            {
                                "positive_prompt": (
                                    "Create a clean character design of детектив"
                                ),
                                "negative_prompt": "",
                            },
                            4,
                        )

        translate_call = session.post.call_args_list[0]
        image_call = session.post.call_args_list[1]

        self.assertIn(":generateContent", translate_call.args[0])
        self.assertIn(
            "детектив",
            translate_call.kwargs["json"]["contents"][0]["parts"][0]["text"],
        )
        self.assertEqual(
            image_call.kwargs["json"]["instances"][0]["prompt"],
            "Create a clean character design of a detective",
        )

    def test_blocked_translation_returns_user_facing_error(self):
        blocked_response = Mock()
        blocked_response.raise_for_status.return_value = None
        blocked_response.json.return_value = {
            "promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
            "usageMetadata": {"promptTokenCount": 136},
        }
        session = Mock()
        session.post.return_value = blocked_response

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False):
            with patch(PROVIDER_SESSION, return_value=session):
                with self.assertRaises(ProviderContentBlockedError) as ctx:
                    GeminiProvider().generate_character_variants(
                        SimpleNamespace(job_id=uuid4()),
                        {
                            "positive_prompt": "Create a clean character design of персонаж",
                            "negative_prompt": "",
                        },
                        4,
                    )

        self.assertEqual(ctx.exception.error_code, "GEMINI_PROHIBITED_CONTENT")
        self.assertIn("Gemini заблокировал промпт", ctx.exception.user_message)
        self.assertNotIn("promptFeedback", str(ctx.exception))


class CharacterStudioApiTests(CharacterStudioTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = str(self.user_key.key)

    def test_api_flow(self):
        create = self.client.post(
            f"/api/projects/{self.project.id}/characters",
            {"token_user": self.token, "name": "Mira", "age": 17, "visual_style": "anime"},
            format="json",
        )
        self.assertEqual(create.status_code, 201)
        character_id = create.json()["character_id"]

        job_response = self.client.post(
            f"/api/projects/{self.project.id}/characters/{character_id}/generate-initial-variants",
            {"token_user": self.token, "variant_count": 4},
            format="json",
        )
        self.assertEqual(job_response.status_code, 200)
        job_id = job_response.json()["job_id"]
        job = self.client.get(f"/api/generation-jobs/{job_id}", {"token_user": self.token})
        job_data = job.json()
        self.assertEqual(len(job_data["variants"]), 4)

        variant_id = job_data["variants"][0]["variant_id"]
        apply = self.client.post(
            f"/api/projects/{self.project.id}/characters/{character_id}/apply-variant",
            {"token_user": self.token, "variant_id": variant_id, "apply_as": "current_reference"},
            format="json",
        )
        self.assertEqual(apply.status_code, 201)

        lock = self.client.post(
            f"/api/projects/{self.project.id}/characters/{character_id}/lock-identity",
            {"token_user": self.token, "confirm": True},
            format="json",
        )
        self.assertEqual(lock.status_code, 200)

    def test_permission_rejected(self):
        other = UserKey.objects.create(user=User.objects.create_user(username="other"))
        response = self.client.get(f"/api/projects/{self.project.id}/characters", {"token_user": str(other.key)})
        self.assertEqual(response.status_code, 403)

    def test_empty_character_list_returns_json_array(self):
        response = self.client.get(
            f"/api/projects/{self.project.id}/characters",
            {"token_user": self.token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_unknown_outfit_returns_404(self):
        character = self.create_character()

        response = self.client.patch(
            f"/api/projects/{self.project.id}/characters/{character.character_id}/outfits/{uuid4()}",
            {"token_user": self.token, "name": "Missing"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error_code"], "NOT_FOUND")
