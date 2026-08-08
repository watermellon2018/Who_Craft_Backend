"""LiteLLMProvider tests with the ``litellm`` module fully mocked.

These run even when litellm isn't installed — we inject a stub into
``sys.modules`` before the provider imports it.
"""

from __future__ import annotations

import base64
import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase

from django.test import override_settings

from w_craft_back.services.image_generation.errors import (
    CODE_BAD_RESPONSE,
    CODE_EDIT_NOT_SUPPORTED,
    ImageProviderError,
)
from w_craft_back.services.image_generation.litellm_provider import (
    LiteLLMProvider,
    _decode_b64_or_data_url,
    _extract_chat_images,
    _extract_image_api,
)
from w_craft_back.services.image_generation.registry import MODEL_REGISTRY
from w_craft_back.storage_gateway import normalize_image_bytes


def _install_litellm_stub(image_generation=None, completion=None, exceptions=None):
    module = ModuleType("litellm")
    module.image_generation = image_generation or (lambda **_: None)
    module.completion = completion or (lambda **_: None)
    exc_module = exceptions or ModuleType("litellm.exceptions")
    module.exceptions = exc_module
    sys.modules["litellm"] = module
    sys.modules["litellm.exceptions"] = exc_module
    return module


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
    "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
NORMALIZED_PNG_BYTES = normalize_image_bytes(PNG_BYTES).data
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


class ExtractorTest(TestCase):
    def test_extract_image_api_b64(self):
        resp = {"data": [{"b64_json": PNG_B64}, {"b64_json": PNG_B64}]}
        images = _extract_image_api(resp)
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0], NORMALIZED_PNG_BYTES)

    def test_extract_image_api_data_url(self):
        resp = {"data": [{"url": f"data:image/png;base64,{PNG_B64}"}]}
        images = _extract_image_api(resp)
        self.assertEqual(images[0], NORMALIZED_PNG_BYTES)

    def test_extract_image_api_rejects_remote_url(self):
        with self.assertRaises(ImageProviderError) as cm:
            _extract_image_api(
                {"data": [{"url": "https://attacker.test/image.png"}]}
            )
        self.assertEqual(cm.exception.code, CODE_BAD_RESPONSE)

    @override_settings(IMAGE_PROVIDER_MAX_OUTPUT_BYTES=4)
    def test_decode_rejects_oversized_provider_image(self):
        encoded = base64.b64encode(b"12345").decode("ascii")
        with self.assertRaises(ImageProviderError) as cm:
            _decode_b64_or_data_url(encoded)
        self.assertEqual(cm.exception.code, CODE_BAD_RESPONSE)

    @override_settings(IMAGE_PROVIDER_MAX_OUTPUT_IMAGES=1)
    def test_extract_image_api_rejects_too_many_images(self):
        response = {"data": [{"b64_json": PNG_B64}, {"b64_json": PNG_B64}]}
        with self.assertRaises(ImageProviderError) as cm:
            _extract_image_api(response)
        self.assertEqual(cm.exception.code, CODE_BAD_RESPONSE)

    @override_settings(
        IMAGE_PROVIDER_MAX_OUTPUT_BYTES=10,
        IMAGE_PROVIDER_MAX_OUTPUT_TOTAL_BYTES=5,
    )
    def test_extract_image_api_rejects_aggregate_output_over_limit(self):
        encoded = base64.b64encode(b"123").decode("ascii")
        response = {"data": [{"b64_json": encoded}, {"b64_json": encoded}]}
        with self.assertRaises(ImageProviderError) as cm:
            _extract_image_api(response)
        self.assertEqual(cm.exception.code, CODE_BAD_RESPONSE)

    def test_extract_image_api_empty_raises(self):
        with self.assertRaises(ImageProviderError) as cm:
            _extract_image_api({"data": []})
        self.assertEqual(cm.exception.code, CODE_BAD_RESPONSE)

    def test_extract_chat_images_from_message_images(self):
        resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            images=[{"image_url": {"url": f"data:image/png;base64,{PNG_B64}"}}],
            content="",
        ))])
        images = _extract_chat_images(resp)
        self.assertEqual(images, [NORMALIZED_PNG_BYTES])

    def test_extract_chat_images_from_content_parts(self):
        resp = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{PNG_B64}"
                                },
                            },
                            {"type": "text", "text": "ignored"},
                        ],
                    }
                }
            ]
        }
        images = _extract_chat_images(resp)
        self.assertEqual(images, [NORMALIZED_PNG_BYTES])

    def test_extract_chat_images_empty_raises_bad_response(self):
        resp = {
            "choices": [
                {
                    "message": {
                        "content": [{"type": "text", "text": "no image"}]
                    }
                }
            ]
        }
        with self.assertRaises(ImageProviderError) as cm:
            _extract_chat_images(resp)
        self.assertEqual(cm.exception.code, CODE_BAD_RESPONSE)


class GenerateTest(TestCase):
    def tearDown(self):
        sys.modules.pop("litellm", None)
        sys.modules.pop("litellm.exceptions", None)

    def test_image_mode_invokes_image_generation(self):
        captured: dict = {}

        def fake_image_generation(**kwargs):
            captured.update(kwargs)
            return {"data": [{"b64_json": PNG_B64}]}

        _install_litellm_stub(image_generation=fake_image_generation)

        provider = LiteLLMProvider(MODEL_REGISTRY["gemini-imagen-4"])
        images = provider.generate("a cat", aspect_ratio="1:1", variant_count=1)

        self.assertEqual(images, [NORMALIZED_PNG_BYTES])
        self.assertEqual(captured["model"], "gemini/imagen-4.0-generate-001")
        self.assertEqual(captured["prompt"], "a cat")
        self.assertEqual(captured["n"], 1)
        self.assertEqual(captured["size"], "1024x1024")
        self.assertEqual(captured["response_format"], "b64_json")

    def test_chat_mode_invokes_completion(self):
        captured: dict = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return {
                "choices": [{"message": {
                    "images": [
                        {
                            "image_url": {
                                "url": f"data:image/png;base64,{PNG_B64}"
                            }
                        }
                    ],
                    "content": "",
                }}]
            }

        _install_litellm_stub(completion=fake_completion)

        provider = LiteLLMProvider(MODEL_REGISTRY["gemini-flash-image"])
        images = provider.generate("a dog", aspect_ratio="3:4")

        self.assertEqual(images, [NORMALIZED_PNG_BYTES])
        self.assertEqual(captured["model"], "gemini/gemini-2.5-flash-image")
        self.assertEqual(captured["modalities"], ["image", "text"])


class EditTest(TestCase):
    def tearDown(self):
        sys.modules.pop("litellm", None)
        sys.modules.pop("litellm.exceptions", None)

    def test_edit_unsupported_raises_edit_not_supported(self):
        _install_litellm_stub()
        provider = LiteLLMProvider(MODEL_REGISTRY["gemini-imagen-4"])
        with self.assertRaises(ImageProviderError) as cm:
            provider.edit(b"abc", "make it red")
        self.assertEqual(cm.exception.code, CODE_EDIT_NOT_SUPPORTED)

    def test_edit_passes_inline_image_to_completion(self):
        captured: dict = {}

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {
                "content": [{"type": "image_url",
                             "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}}],
            }}]}

        _install_litellm_stub(completion=fake_completion)

        provider = LiteLLMProvider(MODEL_REGISTRY["gemini-flash-image"])
        result = provider.edit(PNG_BYTES, "edit it", mime_type="image/png")

        self.assertEqual(result, NORMALIZED_PNG_BYTES)
        content = captured["messages"][0]["content"]
        # first part = image, second part = instruction
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(
            content[0]["image_url"]["url"].startswith("data:image/png;base64,")
        )
        self.assertEqual(content[1]["type"], "text")
        self.assertEqual(content[1]["text"], "edit it")


class ErrorMappingTest(TestCase):
    def tearDown(self):
        sys.modules.pop("litellm", None)
        sys.modules.pop("litellm.exceptions", None)

    def test_litellm_auth_error_maps_to_forbidden(self):
        # Build a stub exceptions module
        class AuthenticationError(Exception):
            pass

        exc_module = ModuleType("litellm.exceptions")
        exc_module.AuthenticationError = AuthenticationError

        def fake_image_generation(**_):
            raise AuthenticationError("bad key")

        _install_litellm_stub(
            image_generation=fake_image_generation, exceptions=exc_module,
        )

        provider = LiteLLMProvider(MODEL_REGISTRY["gemini-imagen-4"])
        with self.assertRaises(ImageProviderError) as cm:
            provider.generate("hi")
        self.assertEqual(cm.exception.code, "IMAGE_PROVIDER_FORBIDDEN")
