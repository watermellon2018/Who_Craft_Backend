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
from w_craft_back.character_studio.models import CharacterAsset, CharacterAssetType, CharacterGenerationJob, CharacterImage, CharacterOutfit, CharacterStatus
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
                "role": "main",
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
            {"role": "antagonist", "speech_style": "dry"},
        )
        self.assertEqual(updated.role, "antagonist")
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

        self.assertEqual(len(jobs), 3)
        self.assertEqual([job.request_payload["image_type"] for job in jobs], ["portrait", "full_body", "scene"])
        self.assertEqual(
            set(CharacterImage.objects.filter(character=character, is_active=True).values_list("image_type", flat=True)),
            {"portrait", "full_body", "scene"},
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


# ---------------------------------------------------------------------------
# Scenario 1: Portrait selection preserves character_id and creates CharacterImage
# ---------------------------------------------------------------------------


class PortraitSelectionTests(CharacterStudioTestCase):
    """After a user selects a portrait variant the editor must find a canonical
    CharacterImage for the portrait type and the character_id must remain stable."""

    def test_apply_variant_portrait_creates_portrait_image(self):
        character = self.create_character()
        job = CharacterGenerationService().create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {"variant_count": 1, "image_type": "portrait"},
        )
        variant = job.variants.first()
        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            character.character_id,
            variant.variant_id,
            {"apply_as": "current_reference", "image_type": "portrait"},
        )
        self.assertTrue(
            CharacterImage.objects.filter(
                character=character, image_type="portrait", is_active=True
            ).exists(),
            "No active portrait CharacterImage found after apply_variant with image_type='portrait'",
        )

    def test_character_id_preserved_after_apply_variant(self):
        character = self.create_character()
        original_id = character.character_id
        job = CharacterGenerationService().create_initial_variants(
            self.user_key, self.project.id, character.character_id, {"variant_count": 1}
        )
        variant = job.variants.first()
        revision = CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            character.character_id,
            variant.variant_id,
            {"apply_as": "current_reference"},
        )
        character.refresh_from_db()
        self.assertEqual(character.character_id, original_id)
        self.assertEqual(str(revision.character_id), str(original_id))

    def test_apply_variant_does_not_create_extra_generation_jobs(self):
        """apply_variant must NOT trigger any new generation jobs by itself."""
        character = self.create_character()
        gen_job = CharacterGenerationService().create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {"variant_count": 1, "image_type": "portrait"},
        )
        variant = gen_job.variants.first()
        jobs_before = CharacterGenerationJob.objects.filter(character=character).count()

        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            character.character_id,
            variant.variant_id,
            {"apply_as": "current_reference", "image_type": "portrait"},
        )

        self.assertEqual(
            CharacterGenerationJob.objects.filter(character=character).count(),
            jobs_before,
            "apply_variant must not create new generation jobs",
        )


# ---------------------------------------------------------------------------
# Scenario 2: Secondary asset generation via generate_edit_variants
# ---------------------------------------------------------------------------


class EditorSecondaryAssetTests(CharacterStudioTestCase):
    """The editor auto-launches generate_edit_variants for full_body / scene /
    reference_sheet.  Each call must produce exactly one active CharacterImage
    of the correct type."""

    def _generate_secondary(self, character, image_type, region):
        return CharacterGenerationService().generate_edit_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {
                "region": region,
                "image_type": image_type,
                "variant_count": 1,
                "preserve": {},
                "controls": {},
            },
        )

    def test_generate_edit_full_body_creates_character_image(self):
        character = self.create_character()
        job = self._generate_secondary(character, "full_body", "body")
        self.assertEqual(job.status, "completed")
        self.assertEqual(
            CharacterImage.objects.filter(
                character=character, image_type="full_body", is_active=True
            ).count(),
            1,
            "Expected exactly 1 active full_body CharacterImage after generation",
        )

    def test_generate_edit_scene_creates_character_image(self):
        character = self.create_character()
        job = self._generate_secondary(character, "scene", "style")
        self.assertEqual(job.status, "completed")
        self.assertEqual(
            CharacterImage.objects.filter(
                character=character, image_type="scene", is_active=True
            ).count(),
            1,
        )

    def test_generate_edit_reference_sheet_creates_character_image(self):
        character = self.create_character()
        job = self._generate_secondary(character, "reference_sheet", "full_character")
        self.assertEqual(job.status, "completed")
        self.assertEqual(
            CharacterImage.objects.filter(
                character=character, image_type="reference_sheet", is_active=True
            ).count(),
            1,
        )

    def test_repeated_generation_keeps_single_active_image_per_type(self):
        """Retry must deactivate the old image and leave only one active."""
        character = self.create_character()
        self._generate_secondary(character, "full_body", "body")
        self._generate_secondary(character, "full_body", "body")
        self.assertEqual(
            CharacterImage.objects.filter(
                character=character, image_type="full_body", is_active=True
            ).count(),
            1,
            "After two generations of full_body there must be exactly 1 active CharacterImage",
        )

    def test_secondary_generation_does_not_affect_other_image_types(self):
        """Generating full_body must not change scene / reference_sheet records."""
        character = self.create_character()
        self._generate_secondary(character, "scene", "style")
        self._generate_secondary(character, "full_body", "body")
        self.assertEqual(
            CharacterImage.objects.filter(
                character=character, image_type="scene", is_active=True
            ).count(),
            1,
            "Generating full_body must leave scene image unchanged",
        )


# ---------------------------------------------------------------------------
# Scenario 3: Job polling API response structure
# ---------------------------------------------------------------------------


class EditorJobPollingApiTests(CharacterStudioTestCase):
    """Verify that GET /api/generation-jobs/<id> returns the shape expected by
    the frontend hook (status, progress, variants[].variant_id / image_url)."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = str(self.user_key.key)

    def test_get_job_returns_status_progress_and_variants(self):
        character = self.create_character()
        job = CharacterGenerationService().create_initial_variants(
            self.user_key, self.project.id, character.character_id, {"variant_count": 2}
        )
        response = self.client.get(
            f"/api/generation-jobs/{job.job_id}", {"token_user": self.token}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn(data["status"], ("completed", "failed", "processing", "queued"))
        self.assertIsInstance(data["progress"], int)
        self.assertIsInstance(data["variants"], list)
        self.assertGreater(
            len(data["variants"]), 0, "Completed job must include at least one variant"
        )
        first = data["variants"][0]
        self.assertIn("variant_id", first, "variant_id missing from variant dict")
        self.assertIn("image_url", first, "image_url missing from variant dict")

    def test_get_job_for_other_user_returns_403(self):
        character = self.create_character()
        job = CharacterGenerationService().create_initial_variants(
            self.user_key, self.project.id, character.character_id, {"variant_count": 1}
        )
        other_user_key = UserKey.objects.create(
            user=User.objects.create_user(username="stranger_poller")
        )
        response = self.client.get(
            f"/api/generation-jobs/{job.job_id}",
            {"token_user": str(other_user_key.key)},
        )
        self.assertEqual(response.status_code, 403)

    def test_get_nonexistent_job_returns_404(self):
        response = self.client.get(
            f"/api/generation-jobs/{uuid4()}", {"token_user": self.token}
        )
        self.assertEqual(response.status_code, 404)

    def test_completed_job_has_image_url_in_variants(self):
        """Frontend auto-applies the first completed variant; it must have image_url."""
        character = self.create_character()
        job = CharacterGenerationService().generate_edit_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {
                "region": "body",
                "image_type": "full_body",
                "variant_count": 1,
                "preserve": {},
                "controls": {},
            },
        )
        response = self.client.get(
            f"/api/generation-jobs/{job.job_id}", {"token_user": self.token}
        )
        data = response.json()
        self.assertEqual(data["status"], "completed")
        self.assertTrue(
            data["variants"][0].get("image_url"),
            "Completed full_body job variant must have a non-empty image_url",
        )


# ---------------------------------------------------------------------------
# Scenario 3b: Character GET response includes images for all 4 asset types
# ---------------------------------------------------------------------------


class EditorCharacterGetResponseTests(CharacterStudioTestCase):
    """After create_initial_image_set, GET /api/.../characters/<id> must include
    an 'images' dict with all editor modes so the frontend knows which assets are ready."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = str(self.user_key.key)

    def test_character_get_includes_images_after_full_generation(self):
        character = self.create_character()
        CharacterGenerationService().create_initial_image_set(
            self.user_key, self.project.id, character.character_id, {"variant_count": 1}
        )
        response = self.client.get(
            f"/api/projects/{self.project.id}/characters/{character.character_id}",
            {"token_user": self.token},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        images = data.get("images", {})
        for image_type in ("portrait", "full_body", "scene"):
            self.assertIn(image_type, images, f"'images' dict missing key: {image_type}")
            self.assertTrue(
                images[image_type].get("image_url"),
                f"images.{image_type}.image_url is empty or missing",
            )

    def test_character_get_images_is_empty_before_any_generation(self):
        character = self.create_character()
        response = self.client.get(
            f"/api/projects/{self.project.id}/characters/{character.character_id}",
            {"token_user": self.token},
        )
        data = response.json()
        images = data.get("images", {})
        self.assertIsInstance(images, dict)
        self.assertEqual(
            len(images), 0, "No images should be present before any generation"
        )

    def test_character_get_images_partial_after_single_type_generation(self):
        character = self.create_character()
        CharacterGenerationService().create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {"variant_count": 1, "image_type": "portrait"},
        )
        response = self.client.get(
            f"/api/projects/{self.project.id}/characters/{character.character_id}",
            {"token_user": self.token},
        )
        images = response.json().get("images", {})
        self.assertIn("portrait", images)
        for t in ("full_body", "scene", "reference_sheet"):
            self.assertNotIn(
                t, images, f"'{t}' should not be in images before its job runs"
            )


# ---------------------------------------------------------------------------
# Scenario 6: Retry creates a new job only for the target asset type
# ---------------------------------------------------------------------------


class EditorRetryTests(CharacterStudioTestCase):
    """Retry (second generate_edit_variants call) must create a fresh job,
    keep only one active CharacterImage, and not affect other types."""

    def _generate(self, character, image_type, region):
        return CharacterGenerationService().generate_edit_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {
                "region": region,
                "image_type": image_type,
                "variant_count": 1,
                "preserve": {},
                "controls": {},
            },
        )

    def test_retry_creates_new_job_with_different_id(self):
        character = self.create_character()
        job1 = self._generate(character, "full_body", "body")
        job2 = self._generate(character, "full_body", "body")
        self.assertNotEqual(
            job1.job_id, job2.job_id, "Retry must produce a new job_id"
        )

    def test_retry_keeps_single_active_image(self):
        character = self.create_character()
        self._generate(character, "full_body", "body")
        self._generate(character, "full_body", "body")
        self.assertEqual(
            CharacterImage.objects.filter(
                character=character, image_type="full_body", is_active=True
            ).count(),
            1,
        )

    def test_retry_full_body_does_not_create_jobs_for_other_types(self):
        character = self.create_character()
        jobs_before = CharacterGenerationJob.objects.filter(character=character).count()
        self._generate(character, "full_body", "body")
        self._generate(character, "full_body", "body")
        total_jobs = CharacterGenerationJob.objects.filter(character=character).count()
        self.assertEqual(
            total_jobs - jobs_before,
            2,
            "Exactly 2 new jobs should be created (original + retry), not jobs for other types",
        )


class CharacterStatusLifecycleTests(CharacterStudioTestCase):
    """Tests for Bug 1: character must stay 'draft' until a portrait variant is confirmed."""

    def _create_and_generate(self, variant_count=2):
        character = self.create_character()
        job = CharacterGenerationService().create_initial_variants(
            self.user_key, self.project.id, character.character_id, {"variant_count": variant_count}
        )
        return character, job

    def test_new_character_is_draft(self):
        character = self.create_character()
        self.assertEqual(character.status, CharacterStatus.DRAFT)

    def test_draft_not_in_default_list(self):
        # Draft character must NOT appear in the default character list.
        draft = self.create_character()
        self.assertEqual(draft.status, CharacterStatus.DRAFT)

        result = CharacterService().list_project_characters(self.user_key, self.project.id)
        ids = [c["character_id"] for c in result]
        self.assertNotIn(str(draft.character_id), ids)

    def test_applying_portrait_variant_activates_character(self):
        character, job = self._create_and_generate()
        portrait_variant = job.variants.first()

        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            character.character_id,
            portrait_variant.variant_id,
            {"apply_as": "current_reference", "image_type": "portrait"},
        )

        character.refresh_from_db()
        self.assertEqual(character.status, CharacterStatus.ACTIVE)

    def test_active_character_visible_in_default_list(self):
        character, job = self._create_and_generate()
        portrait_variant = job.variants.first()

        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            character.character_id,
            portrait_variant.variant_id,
            {"apply_as": "current_reference", "image_type": "portrait"},
        )

        result = CharacterService().list_project_characters(self.user_key, self.project.id)
        ids = [c["character_id"] for c in result]
        self.assertIn(str(character.character_id), ids)

    def test_applying_non_portrait_variant_does_not_activate(self):
        character, job = self._create_and_generate()
        variant = job.variants.first()

        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            character.character_id,
            variant.variant_id,
            {"apply_as": "current_reference", "image_type": "full_body"},
        )

        character.refresh_from_db()
        self.assertEqual(character.status, CharacterStatus.DRAFT)

    def test_list_with_status_all_returns_drafts_and_active(self):
        draft = self.create_character()

        active_char, job = self._create_and_generate()
        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            active_char.character_id,
            job.variants.first().variant_id,
            {"apply_as": "current_reference", "image_type": "portrait"},
        )

        all_chars = CharacterService().list_project_characters(
            self.user_key, self.project.id, filters={"status": "all"}
        )
        all_ids = [c["character_id"] for c in all_chars]
        self.assertIn(str(draft.character_id), all_ids)
        self.assertIn(str(active_char.character_id), all_ids)

    def test_list_with_status_draft_returns_only_drafts(self):
        draft = self.create_character()
        active_char, job = self._create_and_generate()
        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            active_char.character_id,
            job.variants.first().variant_id,
            {"apply_as": "current_reference", "image_type": "portrait"},
        )

        draft_chars = CharacterService().list_project_characters(
            self.user_key, self.project.id, filters={"status": "draft"}
        )
        ids = [c["character_id"] for c in draft_chars]
        self.assertIn(str(draft.character_id), ids)
        self.assertNotIn(str(active_char.character_id), ids)

    def test_regenerate_reuses_same_character_no_new_record_created(self):
        # Simulates the "Изменить параметры" re-edit flow: the frontend sends a PATCH
        # to update the existing draft character, then calls generate-initial-variants again.
        # No new StudioCharacter record should be created.
        from w_craft_back.character_studio.models import StudioCharacter

        count_before = StudioCharacter.objects.filter(project=self.project).count()
        character = self.create_character()
        self.assertEqual(StudioCharacter.objects.filter(project=self.project).count(), count_before + 1)

        # Simulate "Изменить параметры": update the same draft, generate new variants
        CharacterService().update_character(
            self.user_key,
            self.project.id,
            character.character_id,
            {"name": "Mira Updated"},
        )
        CharacterGenerationService().create_initial_variants(
            self.user_key, self.project.id, character.character_id, {"variant_count": 2}
        )

        # Still only one new character created during this flow
        self.assertEqual(StudioCharacter.objects.filter(project=self.project).count(), count_before + 1)


# ---------------------------------------------------------------------------
# References stage — board GET, generate, correct, upload, make-primary,
# checklist, proceed-to-3D, identity preservation in compiled prompt.
# ---------------------------------------------------------------------------


class ReferencesStageTests(CharacterStudioTestCase):
    REFERENCE_TYPES = (
        "portrait", "full_body", "three_quarter", "profile", "back_view",
        "emotions", "poses", "outfit_details", "character_sheet",
    )

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = str(self.user_key.key)
        self.character = self.create_character()
        self.character_id = self.character.character_id

    def _url(self, suffix=""):
        return (
            f"/api/projects/{self.project.id}/characters/{self.character_id}/references"
            f"{suffix}"
        )

    def _generate(self, reference_type, **extra):
        return self.client.post(
            self._url("/generate"),
            {"token_user": self.token, "reference_type": reference_type, **extra},
            format="json",
        )

    def test_get_returns_all_nine_reference_types_in_stable_order(self):
        response = self.client.get(self._url(), {"token_user": self.token})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        types = [row["reference_type"] for row in body["references"]]
        self.assertEqual(types, list(self.REFERENCE_TYPES))
        # Without any references yet — every row is missing and proceed is blocked.
        self.assertTrue(all(row["status"] == "missing" for row in body["references"]))
        self.assertFalse(body["can_proceed_to_3d"])
        self.assertIn("missing_portrait", body["proceed_blockers"])
        self.assertIn("missing_full_body", body["proceed_blockers"])
        self.assertIn("missing_back_view", body["proceed_blockers"])
        self.assertIn("missing_profile_or_three_quarter", body["proceed_blockers"])

    def test_generate_creates_ready_asset_with_versioning(self):
        response = self._generate("portrait")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        portrait = next(r for r in body["references"]["references"] if r["reference_type"] == "portrait")
        self.assertEqual(portrait["status"], "ready")
        self.assertEqual(portrait["version"], 1)
        # Regenerate => new version, old asset still exists.
        response2 = self._generate("portrait")
        self.assertEqual(response2.status_code, 200, response2.content)
        portrait2 = next(r for r in response2.json()["references"]["references"] if r["reference_type"] == "portrait")
        self.assertEqual(portrait2["status"], "ready")
        self.assertEqual(portrait2["version"], 2)
        self.assertNotEqual(portrait2["asset_id"], portrait["asset_id"])
        self.assertEqual(
            CharacterAsset.objects.filter(
                character=self.character, asset_type=CharacterAssetType.PORTRAIT,
            ).count(),
            2,
        )

    def test_generate_rejects_unknown_reference_type(self):
        response = self._generate("not_a_thing")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")

    def test_correct_creates_new_version_keeping_old(self):
        self._generate("profile")
        original = CharacterAsset.objects.filter(
            character=self.character, asset_type=CharacterAssetType.PROFILE,
        ).first()
        response = self.client.post(
            self._url(f"/{original.asset_id}/correct"),
            {
                "token_user": self.token,
                "correction_prompt": "fix the side profile, keep identity",
                "preserve_identity": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = CharacterAsset.objects.filter(
            character=self.character, asset_type=CharacterAssetType.PROFILE,
        ).order_by("version")
        self.assertEqual(rows.count(), 2)
        latest = rows.last()
        self.assertEqual(latest.version, 2)
        self.assertIn("identity", latest.correction_prompt)
        self.assertTrue(latest.metadata.get("preserve_identity"))

    def test_upload_creates_uploaded_ready_asset(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgAAIAAAUAAeImBZsAAAAASUVORK5CYII="
        )
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            upload = SimpleUploadedFile("photo.png", png_bytes, content_type="image/png")
            response = self.client.post(
                self._url("/upload"),
                {
                    "token_user": self.token,
                    "reference_type": "outfit_details",
                    "file": upload,
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["reference_type"], "outfit_details")
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["source"], "uploaded")

    def test_upload_rejects_invalid_mime(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("evil.gif", b"GIF89a", content_type="image/gif")
        response = self.client.post(
            self._url("/upload"),
            {"token_user": self.token, "reference_type": "portrait", "file": bad},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")

    def test_make_primary_resets_others(self):
        self._generate("portrait")
        self._generate("full_body")
        portrait = CharacterAsset.objects.get(
            character=self.character, asset_type=CharacterAssetType.PORTRAIT,
        )
        full_body = CharacterAsset.objects.get(
            character=self.character, asset_type=CharacterAssetType.FULL_BODY,
        )
        # Make portrait primary first.
        response = self.client.post(
            self._url(f"/{portrait.asset_id}/make-primary"),
            {"token_user": self.token},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        portrait.refresh_from_db()
        self.assertTrue(portrait.is_primary)
        # Switch to full_body — portrait should be unset.
        response = self.client.post(
            self._url(f"/{full_body.asset_id}/make-primary"),
            {"token_user": self.token},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        portrait.refresh_from_db()
        full_body.refresh_from_db()
        self.assertFalse(portrait.is_primary)
        self.assertTrue(full_body.is_primary)

    def test_proceed_to_3d_blocked_without_required_references(self):
        response = self.client.post(
            self._url("/proceed-to-3d"), {"token_user": self.token}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertFalse(body["can_proceed"])
        self.assertIn("missing_portrait", body["blockers"])

    def test_proceed_to_3d_succeeds_when_required_ready(self):
        for ref_type in ("portrait", "full_body", "profile", "back_view"):
            response = self._generate(ref_type)
            self.assertEqual(response.status_code, 200, response.content)
        response = self.client.post(
            self._url("/proceed-to-3d"), {"token_user": self.token}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["can_proceed"])
        self.assertEqual(body["next_stage"], "3d_model")
        self.character.refresh_from_db()
        self.assertEqual(self.character.status, CharacterStatus.REFERENCES_LOCKED)

    def test_three_quarter_satisfies_profile_requirement(self):
        # profile OR three_quarter is acceptable for the side requirement.
        for ref_type in ("portrait", "full_body", "three_quarter", "back_view"):
            self._generate(ref_type)
        response = self.client.post(
            self._url("/proceed-to-3d"), {"token_user": self.token}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_checklist_patch_persists_user_state(self):
        response = self.client.patch(
            self._url("/checklist"),
            {
                "token_user": self.token,
                "appearance_stable": True,
                "outfit_readable": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["checklist"]["appearance_stable"])
        self.assertTrue(body["checklist"]["outfit_readable"])
        self.character.refresh_from_db()
        self.assertTrue(self.character.references_state["appearance_stable"])

    def test_compiled_prompt_for_back_view_contains_identity_lock_tail(self):
        compiler = CharacterPromptCompiler()
        compiled = compiler.compile(
            character=self.character,
            appearance=self.character.active_appearance,
            outfit=None,
            region="full_character",
            image_type="back_view",
            identity_locked=True,
            preserve_identity=True,
            reference_images=["fake-canonical-id"],
        )
        prompt = compiled["positive_prompt"].lower()
        self.assertIn("back view", prompt)
        self.assertIn("do not change face identity", prompt)
        # canonical reference is included
        self.assertEqual(compiled["reference_image_ids"], ["fake-canonical-id"])
        # Match-the-saved-reference tail is present (reference_images path).
        self.assertIn("saved reference image", prompt)

    def test_generate_missing_creates_jobs_only_for_absent_required(self):
        # Pre-create one ready portrait so the batch endpoint must skip it.
        self._generate("portrait")
        portrait_count_before = CharacterAsset.objects.filter(
            character=self.character, asset_type=CharacterAssetType.PORTRAIT,
        ).count()

        response = self.client.post(
            self._url("/generate-missing"),
            {
                "token_user": self.token,
                "reference_types": [
                    "portrait", "full_body", "three_quarter", "profile", "back_view",
                ],
                "only_missing": True,
                "preserve_identity": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        created_types = {job["reference_type"] for job in body["created_jobs"]}
        skipped_types = {item["reference_type"] for item in body["skipped"]}
        self.assertEqual(created_types, {"full_body", "three_quarter", "profile", "back_view"})
        self.assertEqual(skipped_types, {"portrait"})
        # Portrait was NOT regenerated.
        self.assertEqual(
            CharacterAsset.objects.filter(
                character=self.character, asset_type=CharacterAssetType.PORTRAIT,
            ).count(),
            portrait_count_before,
        )

    def test_generate_missing_is_idempotent(self):
        # First call creates jobs for everything.
        first = self.client.post(
            self._url("/generate-missing"),
            {
                "token_user": self.token,
                "reference_types": ["portrait", "full_body", "profile", "back_view"],
                "only_missing": True,
            },
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.content)
        # All 4 jobs created the first time. The mock provider runs the job
        # synchronously inside _run_job, so the assets are already `ready`
        # by the time the response returns — the second call must skip them.
        second = self.client.post(
            self._url("/generate-missing"),
            {
                "token_user": self.token,
                "reference_types": ["portrait", "full_body", "profile", "back_view"],
                "only_missing": True,
            },
            format="json",
        )
        self.assertEqual(second.status_code, 200, second.content)
        body = second.json()
        self.assertEqual(body["created_jobs"], [])
        skipped_types = {item["reference_type"] for item in body["skipped"]}
        self.assertEqual(skipped_types, {"portrait", "full_body", "profile", "back_view"})
        # Each required type still has exactly one CharacterAsset row.
        for asset_type in (
            CharacterAssetType.PORTRAIT,
            CharacterAssetType.FULL_BODY,
            CharacterAssetType.PROFILE,
            CharacterAssetType.BACK_VIEW,
        ):
            count = CharacterAsset.objects.filter(
                character=self.character, asset_type=asset_type,
            ).count()
            self.assertEqual(count, 1, f"{asset_type} was duplicated by the second batch call")

    def test_generate_missing_rejects_unknown_reference_type(self):
        response = self.client.post(
            self._url("/generate-missing"),
            {"token_user": self.token, "reference_types": ["portrait", "moonwalk"]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")

    def test_generate_missing_rejects_empty_reference_types(self):
        response = self.client.post(
            self._url("/generate-missing"),
            {"token_user": self.token, "reference_types": []},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_compiled_prompt_includes_correction_block(self):
        compiler = CharacterPromptCompiler()
        compiled = compiler.compile(
            character=self.character,
            appearance=self.character.active_appearance,
            outfit=None,
            region="full_character",
            image_type="profile",
            correction_prompt="lift the chin slightly",
            preserve_identity=True,
        )
        self.assertIn("USER CORRECTION", compiled["positive_prompt"])
        self.assertIn("lift the chin slightly", compiled["positive_prompt"])
        self.assertEqual(compiled["metadata"]["correction_prompt"], "lift the chin slightly")
        self.assertTrue(compiled["metadata"]["preserve_identity"])
