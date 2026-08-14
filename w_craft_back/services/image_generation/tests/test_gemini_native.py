"""Direct Gemini provider cost accounting tests without live network calls."""

from unittest import TestCase, mock

from w_craft_back.services.image_generation.gemini_native import (
    GeminiNativeProvider,
)
from w_craft_back.services.image_generation.registry import MODEL_REGISTRY


class GeminiNativeUsageTest(TestCase):
    @mock.patch(
        "w_craft_back.movie.poster.gemini_image.generate_image_via_gemini"
    )
    def test_records_provider_rate_from_actual_usage(self, generate):
        generate.return_value = (
            b"image",
            {"promptTokenCount": 100, "totalTokenCount": 100},
        )
        provider = GeminiNativeProvider(MODEL_REGISTRY["gemini-native"])

        self.assertEqual(provider.generate("portrait"), [b"image"])

        usage = provider.usage_snapshot()
        self.assertEqual(usage["promptTokens"], 100)
        self.assertEqual(usage["costSource"], "provider")
        self.assertGreater(float(usage["costUsd"]), 0)
