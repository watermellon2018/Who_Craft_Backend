from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from w_craft_back.movie.music.providers import MusicProviderError
from w_craft_back.movie.music.providers.model_registry import (
    public_audio_model_catalog,
    resolve_audio_model,
    resolve_legacy_audio_route,
    resolved_from_snapshot,
)


class AudioModelRegistryTests(SimpleTestCase):
    @override_settings(
        GEMINI_API_KEY="direct-key",
        OPENROUTER_API_KEY="router-key",
    )
    def test_lyria_prefers_cheaper_direct_google_route(self):
        resolved = resolve_audio_model("lyria-3-pro")

        self.assertEqual(resolved.route.backend_name, "google-lyria")
        self.assertEqual(resolved.pricing(1).estimated_cost, Decimal("0.08"))

    @override_settings(GEMINI_API_KEY="", OPENROUTER_API_KEY="router-key")
    def test_lyria_uses_openrouter_when_direct_route_is_unconfigured(self):
        resolved = resolve_audio_model("lyria-3-clip")

        self.assertEqual(resolved.route.backend_name, "openrouter-lyria")
        self.assertEqual(resolved.pricing(1).estimated_cost, Decimal("0.0422"))
        self.assertEqual(
            resolved.pricing(1).snapshot["creditPurchaseFeeRate"],
            "0.055",
        )

    @override_settings(GEMINI_API_KEY="", OPENROUTER_API_KEY="")
    def test_unknown_and_unconfigured_models_have_stable_errors(self):
        with self.assertRaises(MusicProviderError) as unknown:
            resolve_audio_model("google/lyria-3-pro-preview")
        self.assertEqual(unknown.exception.code, "MUSIC_MODEL_UNKNOWN")

        with self.assertRaises(MusicProviderError) as unconfigured:
            resolve_audio_model("lyria-3-pro")
        self.assertEqual(unconfigured.exception.code, "MUSIC_MODEL_NOT_CONFIGURED")

    @override_settings(MUSIC_ALLOW_MOCK=False)
    def test_mock_model_is_not_selectable_when_disabled(self):
        catalog = public_audio_model_catalog()
        mock = next(row for row in catalog if row["key"] == "mock")

        self.assertFalse(mock["configured"])
        with self.assertRaises(MusicProviderError) as disabled:
            resolve_audio_model("mock")
        self.assertEqual(disabled.exception.code, "MUSIC_MODEL_NOT_CONFIGURED")
        with self.assertRaises(MusicProviderError) as legacy_disabled:
            resolve_legacy_audio_route("mock", require_configured=True)
        self.assertEqual(
            legacy_disabled.exception.code,
            "MUSIC_MODEL_NOT_CONFIGURED",
        )

    @override_settings(
        MUSIC_DEFAULT_AUDIO_MODEL="lyria-3-pro",
        GEMINI_API_KEY="super-secret",
        OPENROUTER_API_KEY="router-secret",
    )
    def test_public_catalog_exposes_availability_but_not_credentials(self):
        catalog = public_audio_model_catalog()
        pro = next(row for row in catalog if row["key"] == "lyria-3-pro")

        self.assertTrue(pro["configured"])
        self.assertTrue(pro["default"])
        self.assertEqual(pro["routes"][0]["provider"], "google-lyria")
        self.assertNotIn("super-secret", repr(catalog))
        self.assertNotIn("router-secret", repr(catalog))
        self.assertNotIn("GEMINI_API_KEY", repr(catalog))

    @override_settings(GEMINI_API_KEY="direct-key", OPENROUTER_API_KEY="")
    def test_snapshot_deserialization_does_not_read_current_catalog(self):
        snapshot = resolve_audio_model("lyria-3-pro").snapshot(1)

        with patch(
            "w_craft_back.movie.music.providers.model_registry._catalog",
            side_effect=AssertionError("current registry must not be read"),
        ):
            restored = resolved_from_snapshot(snapshot)

        self.assertEqual(restored.model.key, "lyria-3-pro")
        self.assertEqual(restored.route.backend_name, "google-lyria")
        self.assertEqual(restored.route.model_id, "lyria-3-pro-preview")

    def test_legacy_openrouter_clip_fields_preserve_exact_route(self):
        restored = resolve_legacy_audio_route(
            "openrouter-lyria",
            "google/lyria-3-clip-preview",
        )

        self.assertEqual(restored.model.key, "lyria-3-clip")
        self.assertEqual(restored.route.backend_name, "openrouter-lyria")

    @override_settings(ELEVENLABS_API_KEY="key")
    def test_elevenlabs_uses_duration_based_immutable_pricing(self):
        resolved = resolve_audio_model("elevenlabs-music-v2")

        pricing = resolved.pricing(1, duration_seconds=30)
        snapshot = resolved.snapshot(1, duration_seconds=30)

        self.assertEqual(pricing.estimated_cost, Decimal("0.075"))
        self.assertEqual(pricing.snapshot["billingUnit"], "minute")
        self.assertEqual(snapshot["pricing"]["durationSeconds"], 30)
