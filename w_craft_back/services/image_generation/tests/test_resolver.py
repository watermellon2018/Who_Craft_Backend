"""Resolver tests — uses simple dummy user objects, no Django DB required."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import TestCase, mock

from w_craft_back.services.image_generation import resolve_provider_for_user
from w_craft_back.services.image_generation.errors import (
    CODE_MODEL_UNKNOWN,
    CODE_NOT_CONFIGURED,
    ImageProviderError,
)
from w_craft_back.services.image_generation.gemini_native import GeminiNativeProvider
from w_craft_back.services.image_generation.litellm_provider import LiteLLMProvider
from w_craft_back.services.image_generation.resolver import _resolve_key


def _user_with_pref(pref: str | None):
    return SimpleNamespace(
        is_authenticated=True,
        profile=SimpleNamespace(image_generation_model=pref or ""),
    )


class ResolveKeyTest(TestCase):
    def test_override_wins(self):
        user = _user_with_pref("gemini-flash-image")
        with mock.patch.dict(os.environ, {"DEFAULT_IMAGE_MODEL": "gemini-native"}):
            key, source = _resolve_key(user, "openrouter-flash-image")
        self.assertEqual(key, "openrouter-flash-image")
        self.assertEqual(source, "override")

    def test_user_pref_used_when_no_override(self):
        user = _user_with_pref("gemini-flash-image")
        with mock.patch.dict(os.environ, {}, clear=True):
            key, source = _resolve_key(user, None)
        self.assertEqual(key, "gemini-flash-image")
        self.assertEqual(source, "user")

    def test_env_default_used_when_no_user_pref(self):
        user = _user_with_pref("")
        with mock.patch.dict(os.environ, {"DEFAULT_IMAGE_MODEL": "gemini-native"}):
            key, source = _resolve_key(user, None)
        self.assertEqual(key, "gemini-native")
        self.assertEqual(source, "env")

    def test_anonymous_uses_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            key, source = _resolve_key(None, None)
        self.assertEqual(key, "gemini-flash-image")
        self.assertEqual(source, "default")


class ResolveProviderTest(TestCase):
    def test_anonymous_with_gemini_key_returns_litellm(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}, clear=False):
            provider = resolve_provider_for_user(None)
        self.assertIsInstance(provider, LiteLLMProvider)
        self.assertEqual(provider.spec.key, "gemini-flash-image")

    def test_missing_env_key_raises_not_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImageProviderError) as cm:
                resolve_provider_for_user(None, override="openrouter-flash-image")
        self.assertEqual(cm.exception.code, CODE_NOT_CONFIGURED)
        self.assertEqual(cm.exception.http_status, 503)

    def test_unknown_key_raises(self):
        with self.assertRaises(ImageProviderError) as cm:
            resolve_provider_for_user(None, override="totally-fake-model")
        self.assertEqual(cm.exception.code, CODE_MODEL_UNKNOWN)

    def test_gemini_native_routes_to_native_provider(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}):
            provider = resolve_provider_for_user(None, override="gemini-native")
        self.assertIsInstance(provider, GeminiNativeProvider)

    def test_require_edit_filters_imagen(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}):
            with self.assertRaises(ImageProviderError):
                resolve_provider_for_user(None, override="gemini-imagen-4", require_edit=True)
