from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from w_craft_back.credits.services import GenerationPriceUnavailable
from w_craft_back.credits.video import (
    capture_video_generation,
    estimate_video_generation,
    release_video_generation,
    reserve_video_generation,
)


class VideoBillingTests(SimpleTestCase):
    def test_personal_jobs_never_touch_ledger(self):
        job = SimpleNamespace(provider_snapshot={
            "generationAccess": {"source": "personal"},
        })
        with patch("w_craft_back.credits.video.reserve_generation") as reserve:
            self.assertIsNone(reserve_video_generation(job))
            self.assertIsNone(capture_video_generation(job))
            self.assertIsNone(release_video_generation(job))
            reserve.assert_not_called()

    def test_unknown_price_cannot_become_free_craft_generation(self):
        with patch("w_craft_back.credits.video.registry.estimate_cost",
                   return_value=None):
            with self.assertRaises(GenerationPriceUnavailable):
                estimate_video_generation(SimpleNamespace(), 6)

    def test_quote_uses_effective_duration_and_original_provider_price(self):
        spec = SimpleNamespace(provider="gemini", key="veo", provider_model="veo")
        with patch("w_craft_back.credits.video.registry.estimate_cost",
                   return_value=Decimal("0.42")) as price:
            estimate = estimate_video_generation(spec, 6, "720p")
        price.assert_called_once_with(spec, 6, "720p")
        self.assertEqual(estimate.reservation_amount, Decimal("0.42"))
        self.assertEqual(estimate.snapshot["durationSeconds"], 6)

    def test_missing_quote_does_not_reserve_platform_wallet(self):
        job = SimpleNamespace(provider_snapshot={
            "generationAccess": {"source": "platform"},
        })
        with self.assertRaises(GenerationPriceUnavailable):
            reserve_video_generation(job)
