"""Registry-level tests — no Django models, no HTTP, no LiteLLM."""

from __future__ import annotations

import os
from unittest import TestCase, mock

from w_craft_back.services.image_generation.errors import (
    CODE_EDIT_NOT_SUPPORTED,
    CODE_MODEL_UNKNOWN,
    ImageProviderError,
)
from w_craft_back.services.image_generation.registry import (
    MODEL_REGISTRY,
    ModelSpec,
    deserialize_model_spec,
    get_default_key,
    is_configured,
    list_available_models,
    resolve_model,
    serialize_model_spec,
)


class RegistryTest(TestCase):
    def test_default_marked_present(self):
        defaults = [s for s in MODEL_REGISTRY.values() if s.default]
        self.assertEqual(len(defaults), 1, "exactly one default expected")
        self.assertEqual(defaults[0].key, "gemini-flash-image")

    def test_get_default_key_falls_back_when_env_unknown(self):
        with mock.patch.dict(os.environ, {"DEFAULT_IMAGE_MODEL": "does-not-exist"}):
            self.assertEqual(get_default_key(), "gemini-flash-image")

    def test_get_default_key_uses_env_when_valid(self):
        with mock.patch.dict(os.environ, {"DEFAULT_IMAGE_MODEL": "gemini-imagen-4"}):
            self.assertEqual(get_default_key(), "gemini-imagen-4")

    def test_resolve_model_unknown_raises_400(self):
        with self.assertRaises(ImageProviderError) as cm:
            resolve_model("nope")
        self.assertEqual(cm.exception.code, CODE_MODEL_UNKNOWN)
        self.assertEqual(cm.exception.http_status, 400)

    def test_resolve_model_blank_falls_back_to_default(self):
        spec = resolve_model("")
        self.assertEqual(spec.key, "gemini-flash-image")

    def test_resolve_model_require_edit_rejects_imagen(self):
        with self.assertRaises(ImageProviderError) as cm:
            resolve_model("gemini-imagen-4", require_edit=True)
        self.assertEqual(cm.exception.code, CODE_EDIT_NOT_SUPPORTED)

    def test_resolve_model_require_edit_accepts_flash(self):
        spec = resolve_model("gemini-flash-image", require_edit=True)
        self.assertTrue(spec.supports_edit)

    def test_is_configured_reports_env_presence(self):
        spec = MODEL_REGISTRY["openrouter-flash-image"]
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            self.assertFalse(is_configured(spec))
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "x"}):
            self.assertTrue(is_configured(spec))

    def test_openrouter_flash_alias_uses_dedicated_images_api(self):
        spec = MODEL_REGISTRY["openrouter-flash-image"]

        self.assertEqual(spec.backend, "openrouter-images")
        self.assertEqual(spec.mode, "images")
        self.assertEqual(
            spec.model_id,
            "google/gemini-3.1-flash-image-preview",
        )
        self.assertEqual(spec.supported_parameters["n"]["max"], 1)

    def test_list_available_models_includes_all(self):
        with mock.patch(
            "w_craft_back.services.image_generation.registry._dynamic_specs",
            return_value=[],
        ):
            rows = list_available_models()
        keys = {row["key"] for row in rows}
        self.assertEqual(keys, set(MODEL_REGISTRY))
        for row in rows:
            self.assertIn("supports_edit", row)
            self.assertIn("supports_reference", row)
            self.assertIn("supported_parameters", row)
            self.assertIn("backend", row)
            self.assertIn("configured", row)
            self.assertIn("default", row)

    def test_resolve_dynamic_model_uses_catalog(self):
        spec = ModelSpec(
            key="openrouter-images:openai/gpt-image-2",
            label="GPT Image 2",
            backend="openrouter-images",
            model_id="openai/gpt-image-2",
            mode="images",
            supports_generate=True,
            supports_edit=True,
            supports_reference=True,
            requires_env=("OPENROUTER_API_KEY",),
        )
        with mock.patch(
            "w_craft_back.services.image_generation.registry._dynamic_specs",
            return_value=[spec],
        ) as catalog:
            resolved = resolve_model(spec.key)
        self.assertEqual(resolved, spec)
        catalog.assert_called_once_with()

    def test_model_spec_snapshot_round_trip(self):
        spec = ModelSpec(
            key="openrouter-images:openai/gpt-image-2",
            label="GPT Image 2",
            description="Image model",
            backend="openrouter-images",
            model_id="openai/gpt-image-2",
            mode="images",
            supports_generate=True,
            supports_edit=True,
            supports_reference=True,
            supported_parameters={
                "n": {"type": "range", "min": 1, "max": 4},
            },
            input_modalities=("text", "image"),
            output_modalities=("image",),
            requires_env=("OPENROUTER_API_KEY",),
        )
        snapshot = serialize_model_spec(spec)
        self.assertIsInstance(snapshot["input_modalities"], list)
        self.assertEqual(deserialize_model_spec(snapshot), spec)

    def test_snapshot_round_trip_preserves_legacy_backend_for_dispatcher(self):
        spec = ModelSpec(
            key="mock",
            label="Legacy mock",
            backend="mock",
            model_id="mock-character-provider",
            mode="mock",
            supports_generate=True,
            supports_edit=True,
            supports_reference=True,
        )
        self.assertEqual(deserialize_model_spec(serialize_model_spec(spec)), spec)

    def test_legacy_openrouter_chat_snapshot_upgrades_to_images_api(self):
        legacy = ModelSpec(
            key="openrouter-flash-image",
            label="Gemini Flash Image via OpenRouter",
            backend="litellm",
            model_id="openrouter/google/gemini-3.1-flash-image-preview",
            mode="chat",
            supports_generate=True,
            supports_edit=True,
            supports_reference=True,
            supported_parameters={
                "input_references": {"type": "range", "min": 0, "max": 1},
                "n": {"type": "range", "min": 1, "max": 4},
            },
            input_modalities=("text", "image"),
            output_modalities=("image", "text"),
            requires_env=("OPENROUTER_API_KEY",),
        )

        restored = deserialize_model_spec(serialize_model_spec(legacy))

        self.assertEqual(restored, MODEL_REGISTRY["openrouter-flash-image"])
