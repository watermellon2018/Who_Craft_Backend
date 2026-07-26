import base64
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.test import TestCase
from requests import HTTPError
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    CharacterGenerationJob,
    CharacterImage,
    CharacterOutfit,
    CharacterStatus,
)
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.errors import (
    IdentityLockedError,
    NotFoundError,
    SafetyRejectedError,
    ValidationError,
)
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.character_studio.services.model3d_autofit_service import (
    _fallback_iris_color,
    _iris_color,
    _hair_band_color,
    _hair_sample_box,
    body_metrics_from_pose,
    classify_face_shape,
    hair_band_box,
    metrics_from_landmarks,
    profile_metrics_from_landmarks,
    point_in_polygon,
    pose_confidence,
    skin_mask_sample_points,
)
from w_craft_back.character_studio.services.prompt_compiler import (
    CharacterPromptCompiler,
)
from w_craft_back.character_studio.services.providers import (
    GeminiProvider,
    ProviderContentBlockedError,
)
from w_craft_back.character_studio.services.revision_service import (
    CharacterRevisionService,
)
from w_craft_back.character_studio.services.safety import CharacterSafetyService
from w_craft_back.movie.project.models import Project

PROVIDER_SESSION = "w_craft_back.character_studio.services.providers.requests.Session"


class CharacterStudioTestCase(TestCase):
    def setUp(self):
        # DRF throttle counters live in Django's default cache and persist
        # across tests inside the same process, so a class with many upload
        # calls (e.g. ReferencesStageTests) hits 60/min and starts returning
        # 429. Reset between tests so throttling behaves like a cold start.
        cache.clear()
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
        self.service.delete_character(
            self.user_key, self.project.id, character.character_id,
        )
        with self.assertRaises(NotFoundError):
            self.service.get_viewable_character(
                self.user_key, self.project.id, character.character_id,
            )

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
        first = CharacterOutfit.objects.create(
            character=character, name="School", is_default=True,
        )
        second = CharacterOutfit.objects.create(character=character, name="Street")
        from w_craft_back.character_studio.repositories.repositories import (
            OutfitRepository,
        )
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
        revision = revision_service.create_revision(
            character, "manual_update", change_summary="checkpoint",
        )
        restored = revision_service.restore_revision(character, revision)
        self.assertEqual(restored.change_type, "restore_revision")
        self.assertEqual(character.revisions.count(), 3)


class GenerationFlowTests(CharacterStudioTestCase):
    def test_full_generation_apply_lock_edit_restore_flow(self):
        character = self.create_character()
        generation = CharacterGenerationService()
        initial_job = generation.create_initial_variants(
            self.user_key, self.project.id, character.character_id,
            {"variant_count": 4},
        )
        self.assertEqual(initial_job.status, "completed")
        self.assertEqual(initial_job.variants.count(), 4)

        variant = initial_job.variants.first()
        CharacterService().apply_variant(
            self.user_key, self.project.id, character.character_id,
            variant.variant_id, {"apply_as": "current_reference"},
        )
        character.refresh_from_db()
        self.assertIsNotNone(character.current_revision)

        CharacterService().lock_identity(
            self.user_key,
            self.project.id,
            character.character_id,
            {
                "reference_image_id": str(character.canonical_reference_image_id),
                "confirm": True,
            },
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
        CharacterService().apply_variant(
            self.user_key, self.project.id, character.character_id,
            hair_variant.variant_id, {"apply_as": "current_reference"},
        )
        previous = character.revisions.order_by("revision_number").first()
        restored = CharacterRevisionService().restore_revision(character, previous)
        self.assertEqual(restored.change_type, "restore_revision")

    def test_generation_validation(self):
        character = self.create_character()
        generation = CharacterGenerationService()
        with self.assertRaises(ValidationError):
            generation.create_initial_variants(
                self.user_key, self.project.id, character.character_id,
                {"variant_count": 3},
            )
        with self.assertRaises(ValidationError):
            generation.generate_edit_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {"region": "hair", "text_refinement": "x" * 501, "variant_count": 4},
            )
        CharacterService().lock_identity(
            self.user_key, self.project.id, character.character_id,
            {"confirm": True},
        )
        with self.assertRaises(IdentityLockedError):
            generation.generate_edit_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {
                    "region": "face",
                    "controls": {"face_shape": "square"},
                    "variant_count": 4,
                },
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
        self.assertTrue(
            character.assets.get(asset_id=variants[1].asset_id).is_canonical,
        )

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

        portrait_image = CharacterImage.objects.get(
            character=character, image_type="portrait", is_active=True,
        )
        full_body_image = CharacterImage.objects.get(
            character=character, image_type="full_body", is_active=True,
        )
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

        self.assertEqual(
            CharacterImage.objects.filter(
                character=character, image_type="portrait", is_active=True,
            ).count(),
            1,
        )
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
        self.assertEqual(
            [job.request_payload["image_type"] for job in jobs],
            ["portrait", "full_body", "scene"],
        )
        self.assertEqual(
            set(
                CharacterImage.objects.filter(
                    character=character, is_active=True,
                ).values_list("image_type", flat=True)
            ),
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
                            "positive_prompt":
                                "Create a clean character design of персонаж",
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
            {
                "token_user": self.token,
                "name": "Mira",
                "age": 17,
                "visual_style": "anime",
            },
            format="json",
        )
        self.assertEqual(create.status_code, 201)
        character_id = create.json()["character_id"]

        job_response = self.client.post(
            f"/api/projects/{self.project.id}/characters/{character_id}"
            "/generate-initial-variants",
            {"token_user": self.token, "variant_count": 4},
            format="json",
        )
        self.assertEqual(job_response.status_code, 200)
        job_id = job_response.json()["job_id"]
        job = self.client.get(
            f"/api/generation-jobs/{job_id}", HTTP_X_USER_TOKEN=self.token,
        )
        job_data = job.json()
        self.assertEqual(len(job_data["variants"]), 4)

        variant_id = job_data["variants"][0]["variant_id"]
        apply = self.client.post(
            f"/api/projects/{self.project.id}/characters/{character_id}/apply-variant",
            {
                "token_user": self.token,
                "variant_id": variant_id,
                "apply_as": "current_reference",
            },
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
        response = self.client.get(
            f"/api/projects/{self.project.id}/characters",
            HTTP_X_USER_TOKEN=str(other.key),
        )
        self.assertEqual(response.status_code, 403)

    def test_empty_character_list_returns_json_array(self):
        response = self.client.get(
            f"/api/projects/{self.project.id}/characters",
            HTTP_X_USER_TOKEN=self.token,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_unknown_outfit_returns_404(self):
        character = self.create_character()

        response = self.client.patch(
            f"/api/projects/{self.project.id}/characters/"
            f"{character.character_id}/outfits/{uuid4()}",
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
            "No active portrait CharacterImage found after apply_variant"
            " with image_type='portrait'",
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
            "After two generations of full_body there must be exactly 1"
            " active CharacterImage",
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
            f"/api/generation-jobs/{job.job_id}", HTTP_X_USER_TOKEN=self.token
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
            HTTP_X_USER_TOKEN=str(other_user_key.key),
        )
        self.assertEqual(response.status_code, 403)

    def test_get_nonexistent_job_returns_404(self):
        response = self.client.get(
            f"/api/generation-jobs/{uuid4()}", HTTP_X_USER_TOKEN=self.token
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
            f"/api/generation-jobs/{job.job_id}", HTTP_X_USER_TOKEN=self.token
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
    an 'images' dict with all editor modes so the frontend knows which assets
    are ready."""

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
            HTTP_X_USER_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        images = data.get("images", {})
        for image_type in ("portrait", "full_body", "scene"):
            self.assertIn(
                image_type, images, f"'images' dict missing key: {image_type}",
            )
            self.assertTrue(
                images[image_type].get("image_url"),
                f"images.{image_type}.image_url is empty or missing",
            )

    def test_character_get_images_is_empty_before_any_generation(self):
        character = self.create_character()
        response = self.client.get(
            f"/api/projects/{self.project.id}/characters/{character.character_id}",
            HTTP_X_USER_TOKEN=self.token,
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
            HTTP_X_USER_TOKEN=self.token,
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
            "Exactly 2 new jobs should be created (original + retry),"
            " not jobs for other types",
        )


class CharacterListingTests(CharacterStudioTestCase):
    """Smoke tests for the character listing/lifecycle.

    Characters start as ``draft`` (so duplicates from abandoned creation
    attempts don't pollute the gallery/tree) and graduate to ``active`` when
    the user applies their first variant. Default list filtering hides drafts.
    """

    def test_new_character_starts_as_draft(self):
        character = self.create_character()
        self.assertEqual(character.status, CharacterStatus.DRAFT)

    def test_draft_character_is_hidden_from_default_list(self):
        character = self.create_character()
        result = CharacterService().list_project_characters(
            self.user_key, self.project.id,
        )
        ids = [c["character_id"] for c in result]
        self.assertNotIn(str(character.character_id), ids)

    def test_draft_character_is_visible_when_status_all(self):
        character = self.create_character()
        result = CharacterService().list_project_characters(
            self.user_key, self.project.id, {"status": "all"},
        )
        ids = [c["character_id"] for c in result]
        self.assertIn(str(character.character_id), ids)

    def test_applying_a_variant_promotes_draft_to_active(self):
        # Generating + applying a portrait variant is the user's confirmation
        # that this is the character they want — the row should then show up
        # in the default gallery list.
        character = self.create_character()
        job = CharacterGenerationService().create_initial_variants(
            self.user_key, self.project.id, character.character_id,
            {"variant_count": 1},
        )
        variant = job.variants.first()
        self.assertIsNotNone(variant)
        CharacterService().apply_variant(
            self.user_key,
            self.project.id,
            character.character_id,
            variant.variant_id,
            {
                "apply_as": "current_reference",
                "image_type": "portrait",
                "notes": "test",
            },
        )
        character.refresh_from_db()
        self.assertEqual(character.status, CharacterStatus.ACTIVE)
        result = CharacterService().list_project_characters(
            self.user_key, self.project.id,
        )
        ids = [c["character_id"] for c in result]
        self.assertIn(str(character.character_id), ids)

    def test_regenerate_reuses_same_character_no_new_record_created(self):
        # Simulates the "Изменить параметры" re-edit flow: the frontend sends a PATCH
        # to update the existing character, then calls generate-initial-variants again.
        # No new StudioCharacter record should be created.
        from w_craft_back.character_studio.models import StudioCharacter

        count_before = StudioCharacter.objects.filter(project=self.project).count()
        character = self.create_character()
        self.assertEqual(
            StudioCharacter.objects.filter(project=self.project).count(),
            count_before + 1,
        )

        CharacterService().update_character(
            self.user_key,
            self.project.id,
            character.character_id,
            {"name": "Mira Updated"},
        )
        CharacterGenerationService().create_initial_variants(
            self.user_key, self.project.id, character.character_id, {"variant_count": 2}
        )

        self.assertEqual(
            StudioCharacter.objects.filter(project=self.project).count(),
            count_before + 1,
        )


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
        response = self.client.get(self._url(), HTTP_X_USER_TOKEN=self.token)
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
        portrait = next(
            r for r in body["references"]["references"]
            if r["reference_type"] == "portrait"
        )
        self.assertEqual(portrait["status"], "ready")
        self.assertEqual(portrait["version"], 1)
        # Regenerate => new version, old asset still exists.
        response2 = self._generate("portrait")
        self.assertEqual(response2.status_code, 200, response2.content)
        portrait2 = next(
            r for r in response2.json()["references"]["references"]
            if r["reference_type"] == "portrait"
        )
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
        # Identity-anchored generation requires an existing identity asset
        # (portrait), so seed one before generating the profile view.
        self._generate("portrait")
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
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAA"
            "C0lEQVR4nGNgAAIAAAUAAeImBZsAAAAASUVORK5CYII="
        )
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            upload = SimpleUploadedFile(
                "photo.png", png_bytes, content_type="image/png",
            )
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
        self.assertEqual(
            created_types, {"full_body", "three_quarter", "profile", "back_view"},
        )
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
        self.assertEqual(
            skipped_types, {"portrait", "full_body", "profile", "back_view"},
        )
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
            self.assertEqual(
                count, 1, f"{asset_type} was duplicated by the second batch call",
            )

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
        self.assertEqual(
            compiled["metadata"]["correction_prompt"], "lift the chin slightly",
        )
        self.assertTrue(compiled["metadata"]["preserve_identity"])


PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAA"
    "C0lEQVR4nGNgAAIAAAUAAeImBZsAAAAASUVORK5CYII="
)


class CharacterCreateFromReferenceTests(CharacterStudioTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = str(self.user_key.key)

    def _url(self):
        return f"/api/projects/{self.project.id}/characters/from-reference"

    def _png(self, name="ref.png"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(
            name, base64.b64decode(PNG_1X1_B64), content_type="image/png",
        )

    def test_happy_path_creates_character_reference_and_job(self):
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            response = self.client.post(
                self._url(),
                {
                    "token_user": self.token,
                    "name": "Энгри дог",
                    "character_type": "human",
                    "role": "main",
                    "age": "26",
                    "gender": "girl",
                    "variants_count": "1",
                    "use_image_as_identity": "true",
                    "reference_image": self._png(),
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertIn("character", body)
        self.assertIn("reference", body)
        self.assertIn("generation_job", body)
        self.assertEqual(body["character"]["name"], "Энгри дог")
        self.assertEqual(
            body["reference"]["asset_type"], CharacterAssetType.UPLOADED_REFERENCE,
        )
        self.assertEqual(body["reference"]["source"], "uploaded")
        # MockProvider always succeeds.
        self.assertEqual(body["generation_job"]["status"], "completed")
        self.assertEqual(body["generation_job"]["job_type"], "reference_variants")

    def test_missing_file_returns_400(self):
        response = self.client.post(
            self._url(),
            {
                "token_user": self.token,
                "name": "Sasha",
                "character_type": "human",
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")

    def test_invalid_mime_returns_400(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile("evil.gif", b"GIF89a", content_type="image/gif")
        response = self.client.post(
            self._url(),
            {
                "token_user": self.token,
                "name": "Sasha",
                "character_type": "human",
                "reference_image": bad,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")

    def test_foreign_project_returns_403(self):
        other_user = User.objects.create_user(username="intruder", password="x")
        other_key = UserKey.objects.create(user=other_user)
        other_project = Project.objects.create(
            user=other_key, title="Other", format="series", annot="x", desc="y",
        )
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            response = self.client.post(
                f"/api/projects/{other_project.id}/characters/from-reference",
                {
                    "token_user": self.token,
                    "name": "Sasha",
                    "character_type": "human",
                    "reference_image": self._png(),
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error_code"], "PERMISSION_DENIED")

    def test_provider_without_image_input_marks_job_failed(self):
        # GeminiProvider (mode="image", supports_edit=False) doesn't accept image-in.
        from w_craft_back.character_studio.services import providers as providers_module

        original = providers_module.get_image_provider

        def force_gemini(_name="mock"):
            return providers_module.GeminiProvider()

        providers_module.get_image_provider = force_gemini
        # Patch through generation_service's already-bound import too.
        from w_craft_back.character_studio.services import generation_service as gs

        original_gs = gs.get_image_provider
        gs.get_image_provider = force_gemini
        try:
            with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
                response = self.client.post(
                    self._url(),
                    {
                        "token_user": self.token,
                        "name": "Sasha",
                        "character_type": "human",
                        "variants_count": "1",
                        "reference_image": self._png(),
                    },
                    format="multipart",
                )
        finally:
            providers_module.get_image_provider = original
            gs.get_image_provider = original_gs

        # When generation fails, the whole create flow is rolled back: 400 + the
        # provider's error_code surfaced in the body, and no orphaned character
        # left behind to clutter the user's character list.
        self.assertEqual(response.status_code, 400, response.content)
        body = response.json()
        self.assertEqual(body["error_code"], "MODEL_DOES_NOT_SUPPORT_IMAGE_INPUT")
        # No character should have been persisted for this project.
        from w_craft_back.character_studio.models import StudioCharacter

        self.assertFalse(
            StudioCharacter.objects.filter(project=self.project).exists()
        )


class IdentityAnchoredReferenceGenerationTests(CharacterStudioTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = str(self.user_key.key)
        self.character = self.create_character()

    def _references_url(self, suffix=""):
        return (
            f"/api/projects/{self.project.id}/characters/"
            f"{self.character.character_id}/references{suffix}"
        )

    def _generate(self, reference_type):
        return self.client.post(
            self._references_url("/generate"),
            {"token_user": self.token, "reference_type": reference_type},
            format="json",
        )

    def test_full_body_requires_identity_asset(self):
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            response = self._generate("full_body")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error_code"], "IDENTITY_ASSET_REQUIRED")
        # No CharacterAsset of type full_body should have been created.
        self.assertFalse(
            CharacterAsset.objects.filter(
                character=self.character, asset_type=CharacterAssetType.FULL_BODY,
            ).exists()
        )

    def test_full_body_uses_existing_portrait_as_identity(self):
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            portrait_response = self._generate("portrait")
            self.assertEqual(
                portrait_response.status_code, 200, portrait_response.content,
            )
            portrait = CharacterAsset.objects.filter(
                character=self.character, asset_type=CharacterAssetType.PORTRAIT,
            ).first()
            self.assertIsNotNone(portrait)

            response = self._generate("full_body")
        self.assertEqual(response.status_code, 200, response.content)
        full_body = CharacterAsset.objects.filter(
            character=self.character, asset_type=CharacterAssetType.FULL_BODY,
        ).first()
        self.assertIsNotNone(full_body)
        # Identity asset id is the portrait we just made.
        self.assertEqual(
            full_body.metadata.get("source_identity_asset_id"),
            str(portrait.asset_id),
        )
        self.assertTrue(full_body.metadata.get("preserve_identity"))

    def test_canonical_reference_image_wins_over_other_portraits(self):
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            # Generate two portraits; the older one will be promoted to canonical.
            self._generate("portrait")
            self._generate("portrait")
            older_portrait, newer_portrait = list(
                CharacterAsset.objects.filter(
                    character=self.character, asset_type=CharacterAssetType.PORTRAIT,
                ).order_by("version")
            )
            self.character.canonical_reference_image = older_portrait
            self.character.save(update_fields=["canonical_reference_image"])

            from w_craft_back.character_studio.services.character_service import (
                CharacterService,
            )
            picked = CharacterService().get_identity_asset(self.character)
        self.assertEqual(picked.asset_id, older_portrait.asset_id)
        self.assertNotEqual(picked.asset_id, newer_portrait.asset_id)

    def test_uploaded_reference_used_when_no_portrait(self):
        # Simulate a character created via "from-reference" flow: only an
        # UPLOADED_REFERENCE asset exists, no portraits yet.
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            from pathlib import Path

            from django.conf import settings

            rel = (
                f"character-studio/characters/{self.character.character_id}"
                "/source/seed.png"
            )
            abs_path = Path(settings.MEDIA_ROOT) / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(base64.b64decode(PNG_1X1_B64))
            uploaded = CharacterAsset.objects.create(
                character=self.character,
                project=self.character.project,
                user=self.character.user,
                asset_type=CharacterAssetType.UPLOADED_REFERENCE,
                image_url=f"/media/{rel}",
                storage_path=rel,
                mime_type="image/png",
                source="uploaded",
                status=CharacterAssetStatus.READY,
            )

            response = self._generate("full_body")
        self.assertEqual(response.status_code, 200, response.content)
        full_body = CharacterAsset.objects.filter(
            character=self.character, asset_type=CharacterAssetType.FULL_BODY,
        ).first()
        self.assertEqual(
            full_body.metadata.get("source_identity_asset_id"),
            str(uploaded.asset_id),
        )

    def test_portrait_generation_stays_text_only(self):
        # Portrait IS the identity source — must not require one to already exist
        # and must go through the text-only pipeline (job_type=initial_variants).
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            response = self._generate("portrait")
        self.assertEqual(response.status_code, 200, response.content)
        from w_craft_back.character_studio.models import CharacterGenerationJob

        job = CharacterGenerationJob.objects.filter(
            character=self.character,
            request_payload__image_type="portrait",
        ).first()
        self.assertIsNotNone(job)
        self.assertEqual(job.job_type, "initial_variants")


class IdentityAnchoredEditTests(CharacterStudioTestCase):
    """generate_edit_variants must route non-portrait edits through the
    image-to-image (identity-anchored) pipeline whenever an identity asset
    exists, and fall back to text-only otherwise."""

    def setUp(self):
        super().setUp()
        self.character = self.create_character()

    def _edit(self, image_type, region):
        return CharacterGenerationService().generate_edit_variants(
            self.user_key, self.project.id, self.character.character_id,
            {
                "region": region, "image_type": image_type,
                "variant_count": 1, "preserve": {}, "controls": {},
            },
        )

    def _seed_portrait(self):
        # Run a portrait edit; the mock provider persists a real png on disk
        # so identity-anchored downstream calls can read it back.
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            self._edit("portrait", "face")
            asset = CharacterAsset.objects.filter(
                character=self.character, asset_type=CharacterAssetType.PORTRAIT,
                status=CharacterAssetStatus.READY,
            ).first()
            return asset

    def test_edit_full_body_uses_identity_when_portrait_exists(self):
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            self._edit("portrait", "face")  # seed PORTRAIT identity
            portrait = CharacterAsset.objects.filter(
                character=self.character, asset_type=CharacterAssetType.PORTRAIT,
                status=CharacterAssetStatus.READY,
            ).first()
            self.assertIsNotNone(portrait)

            job = self._edit("full_body", "body")
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.job_type, "edit_variants")
        full_body = CharacterAsset.objects.filter(
            character=self.character, asset_type=CharacterAssetType.FULL_BODY,
        ).order_by("-created_at").first()
        self.assertIsNotNone(full_body)
        self.assertEqual(
            full_body.metadata.get("source_identity_asset_id"),
            str(portrait.asset_id),
        )

    def test_edit_scene_uses_identity_when_portrait_exists(self):
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            self._edit("portrait", "face")
            portrait = CharacterAsset.objects.filter(
                character=self.character, asset_type=CharacterAssetType.PORTRAIT,
                status=CharacterAssetStatus.READY,
            ).first()

            job = self._edit("scene", "style")
        self.assertEqual(job.status, "completed")
        scene = CharacterAsset.objects.filter(
            character=self.character, asset_type=CharacterAssetType.SCENE,
        ).order_by("-created_at").first()
        self.assertEqual(
            scene.metadata.get("source_identity_asset_id"),
            str(portrait.asset_id),
        )

    def test_edit_full_body_falls_back_to_text_only_without_identity(self):
        # No portrait, no uploaded reference — identity-anchored path is skipped
        # and the legacy text-only edit takes over so the user still gets a
        # result on a fresh character.
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            job = self._edit("full_body", "body")
        self.assertEqual(job.status, "completed")
        full_body = CharacterAsset.objects.filter(
            character=self.character, asset_type=CharacterAssetType.FULL_BODY,
        ).order_by("-created_at").first()
        self.assertIsNotNone(full_body)
        # No identity asset existed, so the source linkage isn't set.
        self.assertIsNone(full_body.metadata.get("source_identity_asset_id"))

    def test_edit_portrait_stays_text_only(self):
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            job = self._edit("portrait", "face")
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.job_type, "edit_variants")
        portrait = CharacterAsset.objects.filter(
            character=self.character, asset_type=CharacterAssetType.PORTRAIT,
        ).order_by("-created_at").first()
        # Portrait edit must never carry a source_identity_asset_id — portrait
        # IS the identity source.
        self.assertIsNone(portrait.metadata.get("source_identity_asset_id"))


# ---------------------------------------------------------------------------
# 3D model stage — parametric editor state (GET/PUT /model3d)
# ---------------------------------------------------------------------------


class Model3DStageTests(CharacterStudioTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = str(self.user_key.key)
        self.character = self.create_character()

    def _url(self):
        return (
            f"/api/projects/{self.project.id}/characters/"
            f"{self.character.character_id}/model3d"
        )

    def _put(self, params, token=None):
        return self.client.put(
            self._url(),
            {"token_user": token or self.token, "params": params},
            format="json",
        )

    def test_get_returns_empty_params_by_default(self):
        response = self.client.get(self._url(), HTTP_X_USER_TOKEN=self.token)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["params"], {})
        self.assertIn("updated_at", body)

    def test_put_then_get_roundtrip(self):
        params = {
            "torso": {"chestWidth": 0.45, "chestDepth": -0.2},
            "eyes": {"eyeColor": "#244a2a", "eyeTilt": 0.3},
            "skin_details": {"freckles": True},
            "face_shape": {"shape": "heart"},
        }
        response = self._put(params)
        self.assertEqual(response.status_code, 200, response.content)
        self.character.refresh_from_db()
        self.assertEqual(self.character.model3d_params["torso"]["chestWidth"], 0.45)

        get_response = self.client.get(self._url(), HTTP_X_USER_TOKEN=self.token)
        body = get_response.json()
        self.assertEqual(body["params"]["eyes"]["eyeColor"], "#244a2a")
        self.assertIs(body["params"]["skin_details"]["freckles"], True)
        self.assertEqual(body["params"]["face_shape"]["shape"], "heart")

    def test_put_clamps_out_of_range_numbers(self):
        response = self._put({"torso": {"chestWidth": 7.5, "chestDepth": -42}})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["params"]["torso"]["chestWidth"], 1.0)
        self.assertEqual(body["params"]["torso"]["chestDepth"], -1.0)

    def test_put_replaces_the_whole_document(self):
        self._put({"torso": {"chestWidth": 0.5}})
        self._put({"waist": {"waistWidth": -0.3}})
        self.character.refresh_from_db()
        self.assertEqual(
            self.character.model3d_params, {"waist": {"waistWidth": -0.3}},
        )

    def test_put_records_a_revision(self):
        count_before = self.character.revisions.count()
        response = self._put({"waist": {"waistWidth": 0.2}})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.character.revisions.count(), count_before + 1)
        revision = self.character.revisions.order_by("-created_at").first()
        self.assertEqual(revision.change_summary, "model3d_updated")

    def test_put_rejects_malformed_documents(self):
        bad_payloads = [
            [1, 2, 3],
            "not an object",
            {"zone": [1]},
            {"bad zone!": {}},
            {"zone": {"параметр": 1}},
            {"zone": {"p": None}},
            {"zone": {"p": "x" * 65}},
        ]
        for bad in bad_payloads:
            response = self._put(bad)
            self.assertEqual(response.status_code, 400, bad)
            self.assertEqual(response.json()["error_code"], "VALIDATION_ERROR")
        self.character.refresh_from_db()
        self.assertEqual(self.character.model3d_params, {})

    def test_foreign_user_cannot_read_or_write(self):
        intruder = User.objects.create_user(username="intruder", password="x")
        intruder_key = UserKey.objects.create(user=intruder)
        get_response = self.client.get(
            self._url(), HTTP_X_USER_TOKEN=str(intruder_key.key),
        )
        self.assertGreaterEqual(get_response.status_code, 400)
        put_response = self._put(
            {"torso": {"chestWidth": 1}}, token=str(intruder_key.key),
        )
        self.assertGreaterEqual(put_response.status_code, 400)
        self.character.refresh_from_db()
        self.assertEqual(self.character.model3d_params, {})


class Model3DAutofitTests(CharacterStudioTestCase):
    SKIN_RGB = (210, 166, 121)
    HAIR_RGB = (58, 42, 26)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = str(self.user_key.key)
        self.character = self.create_character()

    def _url(self):
        return (
            f"/api/projects/{self.project.id}/characters/"
            f"{self.character.character_id}/model3d/autofit"
        )

    def _post(self, token=None):
        return self.client.post(
            self._url(), {"token_user": token or self.token}, format="json",
        )

    def _create_portrait(self, media_root, size=64):
        """Write a synthetic portrait: skin canvas with a dark hair band on top.

        The geometry matches the service's heuristic face crop (center of
        the frame) so the color assertions hold without any face detector.
        """
        from PIL import Image

        rel_path = f"character-studio/tests/{uuid4().hex}.png"
        abs_path = Path(media_root) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (size, size), self.SKIN_RGB)
        image.paste(self.HAIR_RGB, (0, 0, size, int(size * 0.3)))
        image.save(abs_path)
        return CharacterAsset.objects.create(
            character=self.character,
            project=self.project,
            user=self.user_key,
            asset_type=CharacterAssetType.PORTRAIT,
            status=CharacterAssetStatus.READY,
            storage_path=rel_path,
            image_url=f"/media/{rel_path}",
        )

    def _create_face_portrait(self, media_root, size=256):
        """Write a frontal cartoon face mediapipe FaceMesh can detect.

        Used by the integration test that only runs when mediapipe is
        actually installed; primitives like _create_portrait never trip the
        detector, which is the point of the colors-only fallback test.
        """
        from PIL import Image, ImageDraw

        rel_path = f"character-studio/tests/{uuid4().hex}.png"
        abs_path = Path(media_root) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (size, size), (220, 180, 150))
        d = ImageDraw.Draw(img)
        d.ellipse([60, 40, 196, 220], fill=(225, 188, 158))
        d.ellipse([95, 105, 120, 125], fill=(255, 255, 255))
        d.ellipse([103, 110, 113, 120], fill=(60, 40, 30))
        d.ellipse([136, 105, 161, 125], fill=(255, 255, 255))
        d.ellipse([144, 110, 154, 120], fill=(60, 40, 30))
        d.line([95, 98, 120, 96], fill=(80, 55, 40), width=3)
        d.line([136, 96, 161, 98], fill=(80, 55, 40), width=3)
        d.line([128, 120, 128, 150], fill=(190, 150, 120), width=3)
        d.ellipse([122, 148, 134, 158], fill=(200, 160, 130))
        d.arc([108, 165, 148, 195], start=10, end=170, fill=(150, 80, 80), width=4)
        img.save(abs_path)
        return CharacterAsset.objects.create(
            character=self.character,
            project=self.project,
            user=self.user_key,
            asset_type=CharacterAssetType.PORTRAIT,
            status=CharacterAssetStatus.READY,
            storage_path=rel_path,
            image_url=f"/media/{rel_path}",
        )

    def _create_full_body(self, media_root, size=256):
        """Write a plain full-body asset. Pose never detects a primitive,
        so tests that need real landmarks mock _mediapipe_pose; this just
        gives the pipeline a readable file to open."""
        from PIL import Image

        rel_path = f"character-studio/tests/{uuid4().hex}.png"
        abs_path = Path(media_root) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (size, size * 2), (200, 200, 200)).save(abs_path)
        return CharacterAsset.objects.create(
            character=self.character,
            project=self.project,
            user=self.user_key,
            asset_type=CharacterAssetType.FULL_BODY,
            status=CharacterAssetStatus.READY,
            storage_path=rel_path,
            image_url=f"/media/{rel_path}",
        )

    @staticmethod
    def _mediapipe_available():
        try:
            import mediapipe  # noqa: F401
            return hasattr(mediapipe, "solutions")
        except ImportError:
            return False

    @staticmethod
    def _rgb(hex_color):
        return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))

    @classmethod
    def _color_distance(cls, hex_color, rgb):
        return sum((a - b) ** 2 for a, b in zip(cls._rgb(hex_color), rgb))

    @staticmethod
    def _landmarks(overrides=None):
        """Canonical synthetic face: every metric sits at its neutral point.

        face_width = 0.8 (234↔454); the other distances are chosen so each
        ratio equals the service's canonical value, so individual tests
        only need to perturb the landmarks they care about.
        """
        points = {
            33: (0.232, 0.4), 133: (0.396, 0.4),    # left eye outer/inner
            362: (0.604, 0.4), 263: (0.768, 0.4),   # right eye inner/outer
            61: (0.36, 0.75), 291: (0.64, 0.75),    # mouth corners
            48: (0.42, 0.6), 278: (0.58, 0.6),      # nose wings
            234: (0.1, 0.5), 454: (0.9, 0.5),       # face sides
            10: (0.5, 0.0), 152: (0.5, 1.12),       # forehead / chin
            172: (0.188, 0.75), 397: (0.812, 0.75),  # jaw (gonion)
            168: (0.5, 0.45), 2: (0.5, 0.65),         # nose bridge / base
            0: (0.5, 0.70), 13: (0.5, 0.73),        # upper lip
            14: (0.5, 0.76), 17: (0.5, 0.80),       # lower lip
            175: (0.52, 1.05),                       # jaw underside
        }
        points.update(overrides or {})
        return points

    def test_autofit_uses_profile_reference_for_depth_controls(self):
        from PIL import Image

        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            rel_path = f"character-studio/tests/{uuid4().hex}.png"
            absolute = Path(media_root) / rel_path
            absolute.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (320, 480), (210, 180, 160)).save(absolute)
            profile = CharacterAsset.objects.create(
                character=self.character,
                project=self.project,
                user=self.user_key,
                asset_type=CharacterAssetType.PROFILE,
                status=CharacterAssetStatus.READY,
                storage_path=rel_path,
                image_url=f"/media/{rel_path}",
            )
            profile_points = self._profile_landmarks()
            profile_points[1] = (0.34, 0.40)
            with patch(
                "w_craft_back.character_studio.services.model3d_autofit_service."
                "_mediapipe_landmarks",
                side_effect=[self._landmarks(), profile_points],
            ):
                response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["sources"]["profile"], str(profile.asset_id))
        self.assertEqual(body["sources"]["profile_type"], "profile")
        self.assertGreater(body["params"]["nose"]["noseTip"], 0.5)
        self.assertIn("faceDepth", body["params"]["face_shape"])

    def test_no_references_returns_character_authored_shape(self):
        response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIn("no_portrait", body["warnings"])
        self.assertEqual(body["params"]["hips"]["hipsWidth"], -0.1)
        self.assertEqual(
            body["sources"]["appearance"],
            str(self.character.active_appearance_id),
        )
        self.assertIsNone(body["sources"]["portrait"])
        self.assertIsNone(body["sources"]["full_body"])

    def test_structured_appearance_and_outfit_seed_matching_3d_params(self):
        appearance = self.character.active_appearance
        appearance.face_shape = "heart"
        appearance.skin_tone = "fair"
        appearance.eye_color = "light blue"
        appearance.hair_length = "long"
        appearance.hair_style = "wavy"
        appearance.hair_color = "strawberry blonde"
        appearance.body_type = "slim"
        appearance.posture = "confident"
        appearance.save()
        outfit = CharacterOutfit.objects.create(
            character=self.character,
            name="Navy polo",
            description="navy blue polo shirt",
            color_palette=["#263a55", "#2d2d33"],
            is_default=True,
        )
        self.character.active_outfit = outfit
        self.character.save(update_fields=["active_outfit", "updated_at"])

        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        params = response.json()["params"]
        self.assertEqual(params["face_shape"]["shape"], "heart")
        self.assertEqual(params["skin_color"]["skinTone"], "#f0d8c0")
        self.assertEqual(params["eyes"]["eyeColor"], "#5d9ed1")
        self.assertEqual(params["hair"]["hairStyle"], "long")
        self.assertEqual(params["hair"]["hairLength"], 0.9)
        self.assertEqual(params["hair"]["hairShape"], "wavy")
        self.assertEqual(params["hair"]["hairColor"], "#c98257")
        self.assertEqual(params["waist"]["waistWidth"], -0.25)
        self.assertEqual(params["posture"]["posturePreset"], "confident")
        self.assertEqual(params["clothing_top"]["color"], "#263a55")
        self.assertEqual(params["clothing_top"]["style"], "tshirt")
        self.assertEqual(params["clothing_bottom"]["color"], "#2d2d33")
        self.assertEqual(params["clothing_bottom"]["style"], "shorts")

    def test_outfit_text_selects_editable_clothing_silhouettes(self):
        outfit = CharacterOutfit.objects.create(
            character=self.character,
            name="Street outfit",
            description="black long-sleeve hoodie and blue jeans",
            color_palette=["#232329", "#3b5266"],
            is_default=True,
        )
        self.character.active_outfit = outfit
        self.character.save(update_fields=["active_outfit", "updated_at"])

        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        params = response.json()["params"]
        self.assertEqual(params["clothing_top"]["style"], "long_sleeve")
        self.assertEqual(params["clothing_bottom"]["style"], "trousers")

    def test_structured_hair_color_wins_over_portrait_band_sample(self):
        appearance = self.character.active_appearance
        appearance.hair_color = "strawberry blonde"
        appearance.save(update_fields=["hair_color", "updated_at"])
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["params"]["hair"]["hairColor"], "#c98257")

    def test_portrait_yields_plausible_skin_and_hair_colors(self):
        appearance = self.character.active_appearance
        appearance.hair_color = ""
        appearance.source_description = ""
        appearance.appearance_prompt = ""
        appearance.save()
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            asset = self._create_portrait(media_root)
            response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertNotIn("no_portrait", body["warnings"])
        self.assertEqual(body["sources"]["portrait"], str(asset.asset_id))
        skin = body["params"]["skin_color"]["skinTone"]
        hair = body["params"]["hair"]["hairColor"]
        self.assertRegex(skin, r"^#[0-9a-f]{6}$")
        self.assertRegex(hair, r"^#[0-9a-f]{6}$")
        # The skin sample must resemble the canvas color, not the hair band
        # (and vice versa) — that is what "the crop windows are placed
        # correctly" looks like from the outside.
        self.assertLess(
            self._color_distance(skin, self.SKIN_RGB),
            self._color_distance(skin, self.HAIR_RGB),
        )
        self.assertLess(
            self._color_distance(hair, self.HAIR_RGB),
            self._color_distance(hair, self.SKIN_RGB),
        )

    def test_face_portrait_with_mediapipe_yields_proportions(self):
        # When mediapipe is installed and finds a face, autofit must return
        # real facial-proportion params and NOT the landmarks_unavailable
        # warning — this is the path the user hits in production.
        if not self._mediapipe_available():
            self.skipTest("mediapipe with solutions API not installed")
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_face_portrait(media_root)
            response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertNotIn("landmarks_unavailable", body["warnings"])
        # Facial-proportion zones are populated.
        for zone in ("eyes", "nose", "mouth", "jaw_chin", "face_shape"):
            self.assertIn(zone, body["params"])
        self.assertIn(
            body["params"]["face_shape"]["shape"],
            ("oval", "round", "square", "heart"),
        )

    def test_portrait_without_mediapipe_reports_landmark_warnings(self):
        # mediapipe is an optional dependency and is not installed in CI;
        # the endpoint must still answer 200 and flag the skipped zones.
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            response = self._post()
        body = response.json()
        self.assertEqual(response.status_code, 200, response.content)
        if "landmarks_unavailable" in body["warnings"]:
            self.assertEqual(set(body["params"]["eyes"]), {"eyeColor"})
            self.assertIn("eye_color_unavailable", body["warnings"])

    def test_mediapipe_runtime_failure_degrades_to_warning(self):
        # A detector crash mid-inference must degrade to the same
        # "landmarks_unavailable" path as a missing dependency, not a 500.
        media_root = tempfile.mkdtemp()
        target = (
            "w_craft_back.character_studio.services."
            "model3d_autofit_service._mediapipe_landmarks"
        )
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            with patch(target, side_effect=RuntimeError("graph blew up")):
                response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIn("landmarks_unavailable", body["warnings"])
        # Colors still come back — they don't depend on landmarks.
        self.assertIn("skin_color", body["params"])

    def test_autofit_persists_params_and_sets_flag(self):
        # Autofit now applies (not just suggests): it saves the extracted
        # params and flips model3d_autofit_done so it runs exactly once.
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["autofit_done"])
        self.character.refresh_from_db()
        self.assertTrue(self.character.model3d_autofit_done)
        self.assertIn("skin_color", self.character.model3d_params)

    def test_autofit_is_idempotent_after_first_run(self):
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            self._post()
            # User tweaks the model and saves through the normal PUT path.
            self.character.refresh_from_db()
            self.character.model3d_params = {"torso": {"chestWidth": 0.9}}
            self.character.save(update_fields=["model3d_params"])
            # A second autofit must NOT overwrite the manual edit.
            second = self._post()
        self.assertEqual(second.status_code, 200, second.content)
        body = second.json()
        self.assertIn("already_autofitted", body["warnings"])
        self.assertEqual(body["params"], {"torso": {"chestWidth": 0.9}})
        self.character.refresh_from_db()
        self.assertEqual(self.character.model3d_params, {"torso": {"chestWidth": 0.9}})

    @patch("w_craft_back.character_studio.views.compute_autofit")
    def test_generating_profile_keeps_autofit_retryable(self, compute):
        compute.side_effect = [
            {
                "params": {"nose": {"noseTip": 0.0}},
                "warnings": ["no_profile_reference"],
                "sources": {"profile": None, "profile_pending": True},
            },
            {
                "params": {"nose": {"noseTip": 0.6}},
                "warnings": [],
                "sources": {"profile": "profile-id", "profile_pending": False},
            },
        ]

        first = self._post()
        second = self._post()

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()["autofit_version"], 6)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()["autofit_version"], 7)
        self.assertEqual(second.json()["params"]["nose"]["noseTip"], 0.6)
        self.assertEqual(compute.call_count, 2)

    def test_legacy_autofit_is_upgraded_without_overwriting_saved_values(self):
        self.character.model3d_params = {"torso": {"chestWidth": 0.9}}
        self.character.model3d_autofit_done = True
        self.character.model3d_autofit_version = 0
        self.character.save(update_fields=[
            "model3d_params",
            "model3d_autofit_done",
            "model3d_autofit_version",
        ])

        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        params = response.json()["params"]
        self.assertEqual(params["torso"]["chestWidth"], 0.9)
        self.assertEqual(params["hair"]["hairColor"], "#b9653b")
        self.character.refresh_from_db()
        self.assertEqual(self.character.model3d_autofit_version, 7)

    @patch("w_craft_back.character_studio.views.compute_autofit")
    def test_v2_upgrade_replaces_ignored_face_defaults(self, compute):
        self.character.model3d_params = {
            "nose": {"noseTip": 0.0, "noseWidth": 0.25},
            "eyes": {"eyeSize": 0.0},
            "torso": {"chestWidth": 0.9},
        }
        self.character.model3d_autofit_done = True
        self.character.model3d_autofit_version = 2
        self.character.save(update_fields=[
            "model3d_params",
            "model3d_autofit_done",
            "model3d_autofit_version",
        ])
        compute.return_value = {
            "params": {
                "nose": {"noseTip": 0.7, "noseWidth": 0.9},
                "eyes": {"eyeSize": 0.4},
            },
            "warnings": [],
            "sources": {"profile": "profile-id"},
        }

        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        params = response.json()["params"]
        self.assertEqual(params["nose"]["noseTip"], 0.7)
        self.assertEqual(params["nose"]["noseWidth"], 0.25)
        self.assertEqual(params["eyes"]["eyeSize"], 0.4)
        self.assertEqual(params["torso"]["chestWidth"], 0.9)

    @patch("w_craft_back.character_studio.views.compute_autofit")
    def test_v3_upgrade_refreshes_sampled_colors_only(self, compute):
        self.character.model3d_params = {
            "eyes": {"eyeColor": "#141c2b", "eyeSize": 0.55},
            "hair": {"hairColor": "#f2cab1", "hairLength": 0.8},
            "skin_color": {"skinTone": "#e8bea8", "skinSaturation": 0.3},
        }
        self.character.model3d_autofit_done = True
        self.character.model3d_autofit_version = 3
        self.character.save(update_fields=[
            "model3d_params",
            "model3d_autofit_done",
            "model3d_autofit_version",
        ])
        compute.return_value = {
            "params": {
                "eyes": {"eyeColor": "#5c6f81", "eyeSize": -0.4},
                "hair": {"hairColor": "#da9b6f", "hairLength": 0.2},
                "skin_color": {"skinTone": "#f0c6ad", "skinSaturation": -0.2},
            },
            "warnings": [],
            "sources": {},
        }

        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        params = response.json()["params"]
        self.assertEqual(params["eyes"], {"eyeColor": "#5c6f81", "eyeSize": 0.55})
        self.assertEqual(params["hair"], {"hairColor": "#da9b6f", "hairLength": 0.8})
        self.assertEqual(
            params["skin_color"],
            {"skinTone": "#f0c6ad", "skinSaturation": 0.3},
        )
        self.assertEqual(response.json()["autofit_version"], 7)

    def test_model3d_get_reports_autofit_done(self):
        get_url = (
            f"/api/projects/{self.project.id}/characters/"
            f"{self.character.character_id}/model3d"
        )
        before = self.client.get(get_url, HTTP_X_USER_TOKEN=self.token)
        self.assertFalse(before.json()["autofit_done"])
        self.assertEqual(before.json()["autofit_version"], 0)
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            self._post()
        after = self.client.get(get_url, HTTP_X_USER_TOKEN=self.token)
        self.assertTrue(after.json()["autofit_done"])
        self.assertEqual(after.json()["autofit_version"], 7)

    def test_metrics_canonical_face_is_neutral_oval(self):
        metrics = metrics_from_landmarks(self._landmarks())
        self.assertAlmostEqual(metrics["eyes"]["eyeDistance"], 0.0, places=5)
        self.assertAlmostEqual(metrics["eyes"]["eyeTilt"], 0.0, places=5)
        self.assertAlmostEqual(metrics["eyes"]["eyeSize"], 0.0, places=5)
        self.assertAlmostEqual(metrics["nose"]["noseWidth"], 0.0, places=5)
        self.assertAlmostEqual(metrics["mouth"]["mouthWidth"], 0.0, places=5)
        self.assertAlmostEqual(metrics["jaw_chin"]["jawWidth"], 0.0, places=5)
        self.assertEqual(metrics["face_shape"]["shape"], "oval")

    def test_wide_set_eyes_give_positive_eye_distance(self):
        metrics = metrics_from_landmarks(
            self._landmarks({133: (0.356, 0.4), 362: (0.644, 0.4)})
        )
        self.assertGreater(metrics["eyes"]["eyeDistance"], 0)
        self.assertLessEqual(metrics["eyes"]["eyeDistance"], 1.0)

    def test_raised_outer_corners_give_positive_eye_tilt(self):
        metrics = metrics_from_landmarks(
            self._landmarks({33: (0.232, 0.36), 263: (0.768, 0.36)})
        )
        self.assertGreater(metrics["eyes"]["eyeTilt"], 0)

    def test_short_face_classified_round(self):
        metrics = metrics_from_landmarks(self._landmarks({152: (0.5, 0.45)}))
        self.assertEqual(metrics["face_shape"]["shape"], "round")

    def test_long_face_with_narrow_jaw_classified_heart(self):
        metrics = metrics_from_landmarks(
            self._landmarks({
                152: (0.5, 1.3),
                172: (0.22, 0.75),
                397: (0.78, 0.75),
            })
        )
        self.assertEqual(metrics["face_shape"]["shape"], "heart")

    def test_wide_jaw_classified_square(self):
        metrics = metrics_from_landmarks(
            self._landmarks({172: (0.14, 0.75), 397: (0.86, 0.75)})
        )
        self.assertEqual(metrics["face_shape"]["shape"], "square")
        self.assertGreater(metrics["jaw_chin"]["jawWidth"], 0)

    def test_classify_face_shape_thresholds(self):
        self.assertEqual(classify_face_shape(0.60, 0.78), "round")
        self.assertEqual(classify_face_shape(0.80, 0.70), "heart")
        self.assertEqual(classify_face_shape(0.70, 0.90), "square")
        self.assertEqual(classify_face_shape(0.70, 0.78), "oval")

    @staticmethod
    def _profile_landmarks(mirror=False):
        sign = -1 if mirror else 1
        return {
            10: (0.0, 0.0),
            152: (0.0, 1.0),
            1: (0.24 * sign, 0.40),
            168: (0.045 * sign, 0.25),
            13: (0.075 * sign, 0.55),
            14: (0.075 * sign, 0.58),
            234: (-0.50 * sign, 0.50),
            454: (0.75 * sign, 0.50),
            175: (0.04 * sign, 1.0),
        }

    def test_profile_metrics_are_neutral_at_canonical_depth(self):
        metrics = profile_metrics_from_landmarks(self._profile_landmarks())
        for zone in metrics.values():
            for value in zone.values():
                self.assertAlmostEqual(value, 0.0, delta=0.02)

    def test_profile_metrics_are_mirror_invariant(self):
        left = profile_metrics_from_landmarks(self._profile_landmarks())
        right = profile_metrics_from_landmarks(self._profile_landmarks(mirror=True))
        self.assertEqual(left, right)

    def test_projecting_nose_in_profile_increases_nose_tip(self):
        points = self._profile_landmarks()
        points[1] = (0.34, 0.40)
        metrics = profile_metrics_from_landmarks(points)
        self.assertGreater(metrics["nose"]["noseTip"], 0.5)

    def test_metrics_reject_degenerate_landmarks(self):
        flat = {index: (0.5, 0.5) for index in self._landmarks()}
        with self.assertRaises(ValueError):
            metrics_from_landmarks(flat)

    # ── Body proportions from Pose ──

    @staticmethod
    def _pose(overrides=None):
        """Canonical frontal standing pose: every ratio equals its canonical
        mean, so a clean detection yields 0 on every body slider.

        Geometry: hip width 0.10, shoulder width 0.181 (SH/HIP 1.81), torso
        height 0.25, leg 0.3625 (LEG/TORSO 1.45) split thigh/calf 1.09, arms
        straight down, length 0.2425 (ARM/TORSO 0.97) split upper/forearm
        1.15. Tuples are (x, y, z, visibility).
        """
        cx = 0.5
        sh_y, hip_y = 0.30, 0.55          # torso height 0.25
        sh_hw, hip_hw = 0.0905, 0.05      # half-widths
        thigh, calf = 0.1890, 0.1735      # sum 0.3625, ratio 1.09
        upper, fore = 0.1297, 0.1128      # sum 0.2425, ratio 1.15
        knee_y = hip_y + thigh
        ankle_y = knee_y + calf
        elbow_y = sh_y + upper
        wrist_y = elbow_y + fore
        points = {
            11: (cx + sh_hw, sh_y, 0.0, 1.0), 12: (cx - sh_hw, sh_y, 0.0, 1.0),
            23: (cx + hip_hw, hip_y, 0.0, 1.0), 24: (cx - hip_hw, hip_y, 0.0, 1.0),
            13: (cx + sh_hw, elbow_y, 0.0, 1.0), 14: (cx - sh_hw, elbow_y, 0.0, 1.0),
            15: (cx + sh_hw, wrist_y, 0.0, 1.0), 16: (cx - sh_hw, wrist_y, 0.0, 1.0),
            25: (cx + hip_hw, knee_y, 0.0, 1.0), 26: (cx - hip_hw, knee_y, 0.0, 1.0),
            27: (cx + hip_hw, ankle_y, 0.0, 1.0), 28: (cx - hip_hw, ankle_y, 0.0, 1.0),
        }
        points.update(overrides or {})
        return points

    def test_canonical_pose_yields_neutral_body(self):
        params, warnings = body_metrics_from_pose(self._pose())
        for zone in ("shoulders", "hips", "waist", "thigh", "calf"):
            for value in params[zone].values():
                self.assertAlmostEqual(value, 0.0, places=2)
        # Straight arms → arm length emitted, ~0, no warning.
        self.assertNotIn("arm_length_unavailable", warnings)
        self.assertAlmostEqual(params["upper_arm"]["length"], 0.0, places=2)

    def test_broad_shoulders_narrow_hips_read_as_inverted_triangle(self):
        # Widen shoulders, keep hips → high SH/HIP.
        pose = self._pose({11: (0.62, 0.30, 0.0, 1.0), 12: (0.38, 0.30, 0.0, 1.0)})
        params, _ = body_metrics_from_pose(pose)
        self.assertGreater(params["shoulders"]["shouldersWidth"], 0.1)
        self.assertLess(params["hips"]["hipsWidth"], 0)
        # Engine sign: inverted triangle → negative torsoCurve.
        self.assertLess(params["waist"]["torsoCurve"], 0)

    def test_pear_build_reads_as_positive_torso_curve(self):
        # Narrow shoulders, wide hips → low SH/HIP (pear).
        pose = self._pose({23: (0.59, 0.55, 0.0, 1.0), 24: (0.41, 0.55, 0.0, 1.0)})
        params, _ = body_metrics_from_pose(pose)
        self.assertLess(params["shoulders"]["shouldersWidth"], 0)
        self.assertGreater(params["hips"]["hipsWidth"], 0)
        self.assertGreater(params["waist"]["torsoCurve"], 0)

    def test_long_legs_lengthen_both_segments(self):
        # Push knees and ankles further down → longer legs.
        pose = self._pose({
            25: (0.55, 0.85, 0.0, 1.0), 26: (0.45, 0.85, 0.0, 1.0),
            27: (0.55, 1.15, 0.0, 1.0), 28: (0.45, 1.15, 0.0, 1.0),
        })
        params, _ = body_metrics_from_pose(pose)
        self.assertGreater(params["thigh"]["thighLength"], 0)
        self.assertGreater(params["calf"]["calfLength"], 0)

    def test_bent_arms_drop_arm_length_with_warning(self):
        # Fold both wrists back up near the shoulders → bent elbows.
        pose = self._pose({
            15: (0.59, 0.32, 0.0, 1.0), 16: (0.41, 0.32, 0.0, 1.0),
        })
        params, warnings = body_metrics_from_pose(pose)
        self.assertIn("arm_length_unavailable", warnings)
        self.assertNotIn("upper_arm", params)
        self.assertNotIn("forearm", params)

    def test_pose_confidence_rejects_low_visibility(self):
        pose = self._pose({11: (0.59, 0.30, 0.0, 0.2)})  # one shoulder hidden
        ok, _z = pose_confidence(pose)
        self.assertFalse(ok)

    def test_pose_confidence_rejects_turned_torso(self):
        # Large z-spread between shoulders → subject turned.
        pose = self._pose({
            11: (0.59, 0.30, 0.3, 1.0), 12: (0.41, 0.30, -0.3, 1.0),
        })
        ok, z = pose_confidence(pose)
        self.assertFalse(ok)
        self.assertGreater(z, 0.18)

    def test_pose_confidence_accepts_frontal(self):
        ok, z = pose_confidence(self._pose())
        self.assertTrue(ok)
        self.assertLessEqual(z, 0.18)

    def test_body_metrics_reject_degenerate_pose(self):
        flat = {index: (0.5, 0.5, 0.0, 1.0) for index in self._pose()}
        with self.assertRaises(ValueError):
            body_metrics_from_pose(flat)

    @classmethod
    def _pose_list(cls, overrides=None):
        """The synthetic pose as a 33-length list, the shape
        _mediapipe_pose returns, for mocking it in endpoint tests."""
        points = cls._pose(overrides)
        return [points.get(i, (0.5, 0.5, 0.0, 1.0)) for i in range(33)]

    _POSE_TARGET = (
        "w_craft_back.character_studio.services."
        "model3d_autofit_service._mediapipe_pose"
    )

    def test_autofit_applies_body_metrics_from_full_body(self):
        media_root = tempfile.mkdtemp()
        wide = self._pose_list({
            11: (0.62, 0.30, 0.0, 1.0), 12: (0.38, 0.30, 0.0, 1.0),
        })
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            self._create_full_body(media_root)
            with patch(self._POSE_TARGET, return_value=wide):
                response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        params = response.json()["params"]
        # Broad-shoulder build seeds the silhouette sliders.
        self.assertGreater(params["shoulders"]["shouldersWidth"], 0.1)
        self.assertLess(params["hips"]["hipsWidth"], 0)
        self.character.refresh_from_db()
        self.assertIn("shoulders", self.character.model3d_params)

    def test_autofit_without_full_body_warns(self):
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            response = self._post()
        body = response.json()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("no_full_body", body["warnings"])
        self.assertEqual(body["params"]["shoulders"]["shouldersWidth"], -0.08)

    def test_autofit_skips_body_when_pose_not_frontal(self):
        media_root = tempfile.mkdtemp()
        turned = self._pose_list({
            11: (0.59, 0.30, 0.3, 1.0), 12: (0.41, 0.30, -0.3, 1.0),
        })
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            self._create_full_body(media_root)
            with patch(self._POSE_TARGET, return_value=turned):
                response = self._post()
        body = response.json()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("body_pose_not_frontal", body["warnings"])
        self.assertEqual(body["params"]["shoulders"]["shouldersWidth"], -0.08)

    def test_foreign_user_token_rejected(self):
        intruder = User.objects.create_user(username="intruder", password="x")
        intruder_key = UserKey.objects.create(user=intruder)
        response = self._post(token=str(intruder_key.key))
        self.assertGreaterEqual(response.status_code, 400)

    # ── Skin-mask geometry (pure, mediapipe-free) ──

    @staticmethod
    def _face_landmark_list(size=256):
        """A full 478-point FaceMesh-shaped list describing a frontal face.

        Real ``_mediapipe_landmarks`` returns a position-indexed list of
        (x, y) tuples, so the mask/iris code indexes it by integer — this
        builds one whose contour rings form a clean oval with eyes, brows and
        a mouth inside it, so the skin mask has a real region to sample and
        the holes have somewhere to exclude. Coordinates are normalized.
        """
        import math as _math
        from w_craft_back.character_studio.services import (
            model3d_autofit_service as svc,
        )

        pts = [(0.5, 0.5)] * 478  # default everything to the centre

        def ellipse(ring, cx, cy, rx, ry):
            n = len(ring)
            for i, idx in enumerate(ring):
                a = 2 * _math.pi * i / n
                pts[idx] = (cx + rx * _math.cos(a), cy + ry * _math.sin(a))

        ellipse(svc.FACE_OVAL_RING, 0.5, 0.5, 0.34, 0.46)
        ellipse(svc.LEFT_EYE_RING, 0.63, 0.42, 0.07, 0.035)
        ellipse(svc.RIGHT_EYE_RING, 0.37, 0.42, 0.07, 0.035)
        ellipse(svc.LEFT_EYEBROW_RING, 0.63, 0.36, 0.08, 0.02)
        ellipse(svc.RIGHT_EYEBROW_RING, 0.37, 0.36, 0.08, 0.02)
        ellipse(svc.LIPS_OUTER_RING, 0.5, 0.74, 0.12, 0.05)
        # Key single points the metrics/hair band read.
        pts[svc.FOREHEAD] = (0.5, 0.06)
        pts[svc.CHIN] = (0.5, 0.96)
        pts[svc.FACE_LEFT] = (0.16, 0.5)
        pts[svc.FACE_RIGHT] = (0.84, 0.5)
        pts[svc.LEFT_IRIS_CENTER] = (0.63, 0.42)
        pts[svc.RIGHT_IRIS_CENTER] = (0.37, 0.42)
        return pts

    def test_skin_mask_samples_inside_oval_excluding_features(self):
        from w_craft_back.character_studio.services import (
            model3d_autofit_service as svc,
        )
        pts = self._face_landmark_list()
        w = h = 256
        oval = svc._ring_polygon(pts, svc.FACE_OVAL_RING, w, h)
        samples = skin_mask_sample_points(pts, w, h)
        self.assertGreater(len(samples), 50)
        # Every sampled pixel is inside the oval...
        self.assertTrue(all(point_in_polygon(x, y, oval) for x, y in samples))
        # ...and none lands inside an eye or the mouth.
        for ring, cx, cy in (
            (svc.LEFT_EYE_RING, 0.63, 0.42),
            (svc.RIGHT_EYE_RING, 0.37, 0.42),
            (svc.LIPS_OUTER_RING, 0.5, 0.74),
        ):
            hole = svc._ring_polygon(pts, ring, w, h)
            self.assertFalse(
                any(point_in_polygon(x, y, hole) for x, y in samples),
                f"samples leaked into hole at ({cx},{cy})",
            )

    def test_skin_mask_empty_without_oval_ring(self):
        # No oval contour points → no mask, so the caller falls back to box.
        self.assertEqual(skin_mask_sample_points({}, 256, 256), [])
        self.assertEqual(skin_mask_sample_points(None, 256, 256), [])

    def test_point_in_polygon_basic(self):
        square = [(0, 0), (10, 0), (10, 10), (0, 10)]
        self.assertTrue(point_in_polygon(5, 5, square))
        self.assertFalse(point_in_polygon(15, 5, square))
        self.assertFalse(point_in_polygon(-1, 5, square))

    def test_hair_band_sits_above_forehead_within_skull_width(self):
        pts = self._face_landmark_list()
        w = h = 256
        box = hair_band_box(pts, w, h)
        self.assertIsNotNone(box)
        left, top, right, bottom = box
        forehead_y = pts[10][1] * h
        # The band is above the forehead and has real area.
        self.assertLessEqual(top, forehead_y)
        self.assertAlmostEqual(bottom, forehead_y, delta=1.0)
        self.assertGreater(right - left, 1.0)
        # Horizontal extent stays within the skull width (sides 234↔454).
        self.assertGreaterEqual(left, pts[234][0] * w - 0.5)
        self.assertLessEqual(right, pts[454][0] * w + 0.5)

    def test_hair_band_none_when_forehead_at_frame_top(self):
        pts = self._face_landmark_list()
        pts[10] = (0.5, 0.0)  # forehead pinned to the very top
        self.assertIsNone(hair_band_box(pts, 256, 256))

    def test_fallback_hair_box_samples_crown_instead_of_forehead(self):
        from PIL import Image

        hair = (198, 126, 73)
        skin = (238, 192, 166)
        image = Image.new("RGB", (100, 180), skin)
        image.paste(hair, (30, 9, 70, 29))

        box = _hair_sample_box((30, 45, 70, 135), image.height)
        color = _hair_band_color(image, box)

        self.assertLess(
            self._color_distance(color, hair),
            self._color_distance(color, skin),
        )

    def test_hair_band_color_rejects_bright_highlights(self):
        from PIL import Image

        copper = (190, 110, 60)
        highlight = (242, 202, 177)
        image = Image.new("RGB", (20, 10), copper)
        for x in range(10, 20):
            for y in range(10):
                image.putpixel((x, y), highlight)

        color = _hair_band_color(image, (0, 0, 20, 10))
        self.assertLess(
            self._color_distance(color, copper),
            self._color_distance(color, highlight),
        )

    def test_fallback_iris_color_finds_blue_pair_without_mediapipe(self):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (240, 240), (224, 184, 154))
        draw = ImageDraw.Draw(image)
        for center_x in (86, 154):
            draw.ellipse(
                (center_x - 20, 62, center_x + 20, 82),
                fill=(245, 245, 240),
            )
            draw.ellipse(
                (center_x - 8, 64, center_x + 8, 80),
                fill=(70, 145, 205),
            )
            draw.ellipse(
                (center_x - 3, 67, center_x + 3, 77),
                fill=(10, 15, 22),
            )

        color = _fallback_iris_color(image)

        self.assertIsNotNone(color)
        red, green, blue = self._rgb(color)
        self.assertGreater(blue, red + 35)
        self.assertGreater(green, red + 20)

    def test_fallback_iris_color_rejects_flat_portrait(self):
        from PIL import Image

        image = Image.new("RGB", (240, 240), self.SKIN_RGB)

        self.assertIsNone(_fallback_iris_color(image))

    def test_iris_color_ignores_dark_pupil_and_highlight(self):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (100, 50), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        points = [(0.0, 0.0)] * 478
        for center_index, ring, center_x in (
            (468, (469, 470, 471, 472), 0.3),
            (473, (474, 475, 476, 477), 0.7),
        ):
            pixel_x = int(center_x * image.width)
            pixel_y = int(0.5 * image.height)
            draw.ellipse(
                (pixel_x - 8, pixel_y - 8, pixel_x + 8, pixel_y + 8),
                fill=(80, 150, 220),
            )
            draw.ellipse(
                (pixel_x - 3, pixel_y - 3, pixel_x + 3, pixel_y + 3),
                fill=(8, 8, 12),
            )
            draw.ellipse(
                (pixel_x + 2, pixel_y - 5, pixel_x + 5, pixel_y - 2),
                fill=(255, 255, 255),
            )
            points[center_index] = (center_x, 0.5)
            points[ring[0]] = (center_x - 0.08, 0.5)
            points[ring[1]] = (center_x, 0.34)
            points[ring[2]] = (center_x + 0.08, 0.5)
            points[ring[3]] = (center_x, 0.66)

        color = _iris_color(image, points)
        self.assertIsNotNone(color)
        red, green, blue = self._rgb(color)
        self.assertGreater(blue, red + 80)
        self.assertGreater(green, red + 30)


    # ── Integration: skin colour comes from the masked region ──

    _LANDMARKS_TARGET = (
        "w_craft_back.character_studio.services."
        "model3d_autofit_service._mediapipe_landmarks"
    )

    def _create_masked_portrait(self, media_root, size=256):
        """Portrait where the cheek skin and the lips are different colours.

        Skin fills the frame; a red mouth band sits where the lip ring is and
        a dark brow band where the eyes are. A correct mask samples the skin,
        NOT the red lips — which the old lower-central box would have caught.
        """
        from PIL import Image

        rel_path = f"character-studio/tests/{uuid4().hex}.png"
        abs_path = Path(media_root) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (size, size), self.SKIN_RGB)
        # Red mouth band around y≈0.74 (matches LIPS ring in the fixture).
        for y in range(int(0.69 * size), int(0.79 * size)):
            for x in range(int(0.38 * size), int(0.62 * size)):
                img.putpixel((x, y), (200, 30, 30))
        img.save(abs_path)
        return CharacterAsset.objects.create(
            character=self.character,
            project=self.project,
            user=self.user_key,
            asset_type=CharacterAssetType.PORTRAIT,
            status=CharacterAssetStatus.READY,
            storage_path=rel_path,
            image_url=f"/media/{rel_path}",
        )

    def test_autofit_skin_from_mask_avoids_lips(self):
        media_root = tempfile.mkdtemp()
        landmarks = self._face_landmark_list()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_masked_portrait(media_root)
            with patch(self._LANDMARKS_TARGET, return_value=landmarks):
                response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        skin = body["params"]["skin_color"]["skinTone"]
        # The mask excludes the lips, so the skin reads the canvas colour and
        # is far from the red mouth band.
        self.assertLess(
            self._color_distance(skin, self.SKIN_RGB),
            self._color_distance(skin, (200, 30, 30)),
        )
        self.assertNotIn("skin_segmentation_unavailable", body["warnings"])

    def test_autofit_skin_falls_back_to_box_without_landmarks(self):
        # No landmarks (mediapipe missing/failed) → skin still extracted from
        # the box, flagged with skin_segmentation_unavailable, no crash.
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            self._create_portrait(media_root)
            with patch(self._LANDMARKS_TARGET, return_value=None):
                response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIn("skin_segmentation_unavailable", body["warnings"])
        self.assertIn("hair_segmentation_unavailable", body["warnings"])
        self.assertIn("skin_color", body["params"])
        self.assertIn("hair", body["params"])
