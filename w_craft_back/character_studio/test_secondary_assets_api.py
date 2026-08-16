import os
import tempfile
import time
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import CharacterGenerationJob
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.credits.models import CreditAccount, GenerationCharge
from w_craft_back.movie.project.models import Project


class SecondaryAssetsApiTests(TestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.previous_provider = os.environ.get("CHARACTER_STUDIO_IMAGE_PROVIDER")
        os.environ["CHARACTER_STUDIO_IMAGE_PROVIDER"] = "mock"
        user = User.objects.create_user(username="secondary-owner", password="x")
        self.actor = UserKey.objects.create(user=user)
        self.project = Project.objects.create(
            owner=user,
            title="Secondary assets",
            format="series",
            annotation="Short",
            synopsis="Long",
        )
        self.client = APIClient()
        self.client.credentials(HTTP_X_USER_TOKEN=str(self.actor.key))
        self.character_service = CharacterService()
        self.character = self._create_character("Mira")
        initial_job = CharacterGenerationService().create_initial_variants(
            self.actor,
            self.project.id,
            self.character.character_id,
            {"variant_count": 1, "image_type": "portrait"},
        )
        self.variant = initial_job.variants.get()

    def tearDown(self):
        if self.previous_provider is None:
            os.environ.pop("CHARACTER_STUDIO_IMAGE_PROVIDER", None)
        else:
            os.environ["CHARACTER_STUDIO_IMAGE_PROVIDER"] = self.previous_provider
        self.media_override.disable()
        self.media_directory.cleanup()

    def _create_character(self, name):
        return self.character_service.create_character(
            self.actor,
            self.project,
            {
                "name": name,
                "age": 17,
                "gender": "girl",
                "short_description": "an observant girl",
                "appearance_description": "green eyes and copper hair",
                "visual_style": "cinematic_realism",
            },
        )

    def _quote(self, **overrides):
        body = {
            "variant_id": str(self.variant.variant_id),
            "image_types": ["full_body", "scene"],
            **overrides,
        }
        return self.client.post(self._quote_url(), body, format="json")

    def _quote_url(self, character=None):
        character = character or self.character
        return (
            f"/api/projects/{self.project.id}/characters/"
            f"{character.character_id}/secondary-assets/quote"
        )

    def _generate_url(self, character=None):
        character = character or self.character
        return (
            f"/api/projects/{self.project.id}/characters/"
            f"{character.character_id}/secondary-assets/generate"
        )

    def _apply_portrait(self):
        self.character_service.apply_variant(
            self.actor,
            self.project.id,
            self.character.character_id,
            self.variant.variant_id,
            {"apply_as": "current_reference", "image_type": "portrait"},
        )

    def _generate(self, quote_token, *, key="secondary-batch-key"):
        return self.client.post(
            self._generate_url(),
            {"quote_token": quote_token},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_quote_is_exact_and_reports_wallet_state_before_apply(self):
        response = self._quote()

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(
            [item["image_type"] for item in body["items"]],
            ["full_body", "scene"],
        )
        self.assertEqual(body["available_balance"], "0.000000")
        self.assertTrue(body["sufficient_balance"])
        self.assertFalse(body["account_frozen"])
        self.assertEqual(body["totals"]["reservation_amount"], "0.000000")
        self.assertTrue(body["quote_token"])

    def test_generate_requires_applied_quoted_portrait(self):
        quote = self._quote().json()

        response = self._generate(quote["quote_token"])

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error_code"], "SECONDARY_QUOTE_STALE")

    def test_reapplying_same_canonical_variant_replays_existing_revision(self):
        first_revision = self.character_service.apply_variant(
            self.actor,
            self.project.id,
            self.character.character_id,
            self.variant.variant_id,
            {"apply_as": "current_reference", "image_type": "portrait"},
        )
        image_count = self.character.images.count()
        revision_count = self.character.revisions.count()

        replayed_revision = self.character_service.apply_variant(
            self.actor,
            self.project.id,
            self.character.character_id,
            self.variant.variant_id,
            {"apply_as": "current_reference", "image_type": "portrait"},
        )

        self.assertEqual(replayed_revision.revision_id, first_revision.revision_id)
        self.assertEqual(self.character.images.count(), image_count)
        self.assertEqual(self.character.revisions.count(), revision_count)

    def test_generate_requires_idempotency_key(self):
        quote = self._quote().json()
        self._apply_portrait()

        response = self.client.post(
            self._generate_url(),
            {"quote_token": quote["quote_token"]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            response.json()["error_code"],
            "IDEMPOTENCY_KEY_REQUIRED",
        )

    def test_generate_creates_exact_pair_and_replays_idempotently(self):
        quote = self._quote().json()
        self._apply_portrait()

        first = self._generate(quote["quote_token"])
        replay = self._generate(quote["quote_token"])

        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(replay.status_code, 202, replay.content)
        first_jobs = first.json()["jobs"]
        replay_jobs = replay.json()["jobs"]
        self.assertEqual(
            [row["job_id"] for row in first_jobs],
            [row["job_id"] for row in replay_jobs],
        )
        self.assertEqual(
            [row["image_type"] for row in first_jobs],
            ["full_body", "scene"],
        )
        jobs = CharacterGenerationJob.objects.filter(
            job_id__in=[row["job_id"] for row in first_jobs]
        )
        self.assertEqual(jobs.count(), 2)
        self.assertTrue(all(job.provider_operation == "reference" for job in jobs))
        self.assertEqual(
            GenerationCharge.objects.filter(
                domain="character",
                job_id__in=[row["job_id"] for row in first_jobs],
            ).count(),
            2,
        )

    def test_generate_rejects_tampered_quote(self):
        quote_token = self._quote().json()["quote_token"]
        self._apply_portrait()

        response = self._generate(f"{quote_token}tampered")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error_code"], "SECONDARY_QUOTE_INVALID")

    def test_generate_rejects_expired_quote(self):
        quote_token = self._quote().json()["quote_token"]
        self._apply_portrait()

        with patch("django.core.signing.time.time", return_value=time.time() + 301):
            response = self._generate(quote_token)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error_code"], "SECONDARY_QUOTE_EXPIRED")

    def test_generate_rejects_wrong_character_scope(self):
        quote_token = self._quote().json()["quote_token"]
        other_character = self._create_character("Other")

        response = self.client.post(
            self._generate_url(other_character),
            {"quote_token": quote_token},
            format="json",
            HTTP_IDEMPOTENCY_KEY="wrong-scope-key",
        )

        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(
            response.json()["error_code"],
            "SECONDARY_QUOTE_SCOPE_MISMATCH",
        )

    def test_generate_rejects_quote_after_character_prompt_changes(self):
        quote_token = self._quote().json()["quote_token"]
        self._apply_portrait()
        self.character_service.update_character(
            self.actor,
            self.project.id,
            self.character.character_id,
            {"age": 18},
        )

        response = self._generate(quote_token)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error_code"], "SECONDARY_QUOTE_STALE")

    def test_second_reservation_failure_rolls_back_first_job_and_charge(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            quote = self._quote(
                image_model="openrouter-flash-image",
                routing_mode="manual",
            ).json()
        first_reservation = Decimal(quote["items"][0]["reservation_amount"])
        account = CreditAccount.objects.get(user=self.actor.user)
        account.available_balance = first_reservation
        account.save(update_fields=["available_balance", "updated_at"])
        self._apply_portrait()
        jobs_before = CharacterGenerationJob.objects.count()
        charges_before = GenerationCharge.objects.count()

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            response = self._generate(
                quote["quote_token"], key=f"rollback-{uuid4()}"
            )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error_code"], "INSUFFICIENT_CREDITS")
        self.assertEqual(CharacterGenerationJob.objects.count(), jobs_before)
        self.assertEqual(GenerationCharge.objects.count(), charges_before)
        account.refresh_from_db()
        self.assertEqual(account.available_balance, first_reservation)
        self.assertEqual(account.reserved_balance, Decimal("0"))
