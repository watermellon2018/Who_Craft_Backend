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
    get_default_key,
    is_configured,
    list_available_models,
    resolve_model,
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

    def test_list_available_models_includes_all(self):
        rows = list_available_models()
        keys = {row["key"] for row in rows}
        self.assertEqual(keys, set(MODEL_REGISTRY))
        for row in rows:
            self.assertIn("supports_edit", row)
            self.assertIn("configured", row)
            self.assertIn("default", row)
