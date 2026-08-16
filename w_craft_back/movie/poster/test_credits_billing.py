"""Poster generation wallet integration without provider network calls."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from w_craft_back.credits.models import (
    CreditAccount,
    GenerationCharge,
    GenerationChargeStatus,
)
from w_craft_back.movie.poster.errors import PosterError
from w_craft_back.movie.poster.lifecycle import request_poster_cancellation
from w_craft_back.movie.poster.models import PosterGenerationJob, PosterJobStatus
from w_craft_back.movie.poster.services import enqueue_generation_job
from w_craft_back.movie.project.models import Project
from w_craft_back.services.image_generation.registry import MODEL_REGISTRY


@override_settings(POSTER_GENERATION_USE_MOCK=True)
class PosterGenerationBillingTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="poster-billing")
        self.project = Project.objects.create(
            owner=self.user,
            title="Billing test",
            format="feature_film",
            summary="",
            annotation="",
            synopsis="",
        )
        self.account = CreditAccount.objects.create(
            user=self.user,
            available_balance=Decimal("1.000000"),
        )

    def test_explicit_real_execution_reserves_then_cancel_releases(self):
        provider = SimpleNamespace(
            name="gemini-native",
            model_id="imagen-4.0-generate-001",
            spec=MODEL_REGISTRY["gemini-native"],
        )
        with patch(
            "w_craft_back.movie.poster.services.resolve_provider_for_user",
            return_value=provider,
        ):
            _poster, job, created = enqueue_generation_job(
                project=self.project,
                user=self.user,
                prompt="A dramatic skyline",
                style="cinematic",
                format="vertical",
                idempotency_key="poster-billing-1",
                request_hash="request-hash-1",
                requested_model="gemini-native",
                use_mock=False,
            )

        self.assertTrue(created)
        charge = GenerationCharge.objects.get(domain="poster", job_id=str(job.id))
        self.assertEqual(charge.status, GenerationChargeStatus.RESERVED)
        self.assertEqual(charge.reserved_amount, Decimal("0.040000"))
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance, Decimal("0.960000"))
        self.assertEqual(self.account.reserved_balance, Decimal("0.040000"))

        request_poster_cancellation(job.id)

        charge.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(charge.status, GenerationChargeStatus.RELEASED)
        self.assertEqual(self.account.available_balance, Decimal("1.000000"))
        self.assertEqual(self.account.reserved_balance, Decimal("0.000000"))

    def test_processing_cancel_is_rejected_and_reservation_is_kept(self):
        provider = SimpleNamespace(
            name="gemini-native",
            model_id="imagen-4.0-generate-001",
            spec=MODEL_REGISTRY["gemini-native"],
        )
        with patch(
            "w_craft_back.movie.poster.services.resolve_provider_for_user",
            return_value=provider,
        ):
            _poster, job, _created = enqueue_generation_job(
                project=self.project,
                user=self.user,
                prompt="A paid skyline",
                style="cinematic",
                format="vertical",
                idempotency_key="poster-billing-processing",
                request_hash="request-hash-processing",
                requested_model="gemini-native",
                use_mock=False,
            )
        PosterGenerationJob.objects.filter(pk=job.pk).update(
            status=PosterJobStatus.PROCESSING,
        )

        with self.assertRaisesMessage(PosterError, "only be cancelled while"):
            request_poster_cancellation(job.id)

        charge = GenerationCharge.objects.get(domain="poster", job_id=str(job.id))
        self.account.refresh_from_db()
        self.assertEqual(charge.status, GenerationChargeStatus.RESERVED)
        self.assertEqual(self.account.available_balance, Decimal("0.960000"))
        self.assertEqual(self.account.reserved_balance, Decimal("0.040000"))
