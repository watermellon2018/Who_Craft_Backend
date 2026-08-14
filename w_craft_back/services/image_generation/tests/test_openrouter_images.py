"""Direct OpenRouter Images provider and discovery tests (no live calls)."""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace
from unittest import TestCase, mock

import requests

from w_craft_back.services.image_generation.errors import (
    CODE_BAD_RESPONSE,
    CODE_BLOCKED,
    CODE_FORBIDDEN,
    CODE_UNAVAILABLE,
    ImageProviderError,
)
from w_craft_back.services.image_generation.openrouter_images import (
    OpenRouterImagesProvider,
    clear_openrouter_image_models_cache,
    discover_openrouter_image_models,
)
from w_craft_back.services.image_generation.registry import ModelSpec
from w_craft_back.storage_gateway import normalize_image_bytes

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
    "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
NORMALIZED_PNG_BYTES = normalize_image_bytes(PNG_BYTES).data


def _response(status_code: int, payload, *, text: str = ""):
    response = mock.Mock()
    response.status_code = status_code
    response.text = text
    if isinstance(payload, BaseException):
        response.json.side_effect = payload
    else:
        response.json.return_value = payload
    return response


def _session(*, get_response=None, post_response=None):
    return SimpleNamespace(
        headers={},
        get=mock.Mock(return_value=get_response),
        post=mock.Mock(return_value=post_response),
    )


def _catalog_payload():
    return {
        "data": [
            {
                "id": "openai/gpt-image-2",
                "name": "GPT Image 2",
                "description": "A model",
                "architecture": {
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["image"],
                },
                "supported_parameters": {
                    "n": {"type": "range", "min": 1, "max": 4},
                    "input_references": {
                        "type": "range",
                        "min": 0,
                        "max": 2,
                    },
                    "quality": {
                        "type": "enum",
                        "values": ["auto", "high"],
                    },
                    "future_option": {
                        "type": "future-shape",
                        "nested": {"safe": True},
                    },
                },
                "pricing": {"image": "0.040", "prompt": "0.0000005"},
            },
            {
                "id": "text/not-an-image",
                "name": "Text only",
                "description": "Ignored malformed catalog row",
                "architecture": {
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                },
                "supported_parameters": {},
            },
        ]
    }


def _dynamic_spec(*, supports_reference: bool = True) -> ModelSpec:
    parameters = {
        "n": {"type": "range", "min": 1, "max": 4},
        "aspect_ratio": {"type": "enum", "values": ["1:1", "16:9"]},
        "resolution": {"type": "enum", "values": ["1K", "2K"]},
        "size": {"type": "boolean"},
        "quality": {"type": "enum", "values": ["auto", "high"]},
        "output_format": {"type": "enum", "values": ["png", "webp"]},
        "background": {
            "type": "enum",
            "values": ["auto", "transparent", "opaque"],
        },
        "output_compression": {"type": "range", "min": 0, "max": 100},
        "seed": {"type": "boolean"},
    }
    if supports_reference:
        parameters["input_references"] = {
            "type": "range",
            "min": 0,
            "max": 2,
        }
    return ModelSpec(
        key="openrouter-images:openai/gpt-image-2",
        label="GPT Image 2",
        backend="openrouter-images",
        model_id="openai/gpt-image-2",
        mode="images",
        supports_generate=True,
        supports_edit=supports_reference,
        supports_reference=supports_reference,
        supported_parameters=parameters,
        input_modalities=("text", "image"),
        output_modalities=("image",),
        requires_env=("OPENROUTER_API_KEY",),
    )


class OpenRouterCatalogTest(TestCase):
    def setUp(self):
        clear_openrouter_image_models_cache()

    def tearDown(self):
        clear_openrouter_image_models_cache()

    def test_catalog_parses_capabilities_and_key(self):
        session = _session(get_response=_response(200, _catalog_payload()))
        specs = discover_openrouter_image_models(session=session)

        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.key, "openrouter-images:openai/gpt-image-2")
        self.assertEqual(spec.backend, "openrouter-images")
        self.assertEqual(spec.mode, "images")
        self.assertTrue(spec.supports_reference)
        self.assertTrue(spec.supports_edit)
        self.assertEqual(spec.supported_parameters["input_references"]["max"], 2)
        self.assertEqual(
            spec.supported_parameters["future_option"]["type"],
            "future-shape",
        )
        self.assertEqual(spec.provider_pricing["image"], "0.040")

    def test_catalog_is_cached_for_ttl(self):
        session = _session(get_response=_response(200, _catalog_payload()))
        first = discover_openrouter_image_models(session=session)
        second = discover_openrouter_image_models(session=session)

        self.assertEqual(first, second)
        session.get.assert_called_once()

    def test_catalog_uses_last_known_good_after_refresh_error(self):
        session = _session(get_response=_response(200, _catalog_payload()))
        expected = discover_openrouter_image_models(session=session)
        session.get.side_effect = requests.ConnectionError("offline")

        actual = discover_openrouter_image_models(
            force_refresh=True,
            session=session,
        )
        self.assertEqual(actual, expected)

    def test_catalog_failure_without_cache_is_controlled(self):
        session = _session()
        session.get.side_effect = requests.ConnectionError("offline")
        with self.assertRaises(ImageProviderError) as captured:
            discover_openrouter_image_models(session=session)
        self.assertEqual(captured.exception.code, CODE_UNAVAILABLE)

        with self.assertRaises(ImageProviderError):
            discover_openrouter_image_models(session=session)
        session.get.assert_called_once()

    def test_catalog_does_not_infer_reference_without_descriptor_max(self):
        payload = _catalog_payload()
        payload["data"][0]["supported_parameters"]["input_references"] = {
            "type": "boolean"
        }
        session = _session(get_response=_response(200, payload))
        spec = discover_openrouter_image_models(session=session)[0]
        self.assertFalse(spec.supports_reference)
        self.assertFalse(spec.supports_edit)

    def test_svg_only_model_remains_visible_but_is_not_generatable(self):
        payload = _catalog_payload()
        payload["data"][0]["supported_parameters"]["output_format"] = {
            "type": "enum",
            "values": ["svg"],
        }
        session = _session(get_response=_response(200, payload))
        spec = discover_openrouter_image_models(session=session)[0]
        self.assertFalse(spec.supports_generate)
        self.assertFalse(spec.supports_reference)
        self.assertFalse(spec.supports_edit)


class OpenRouterProviderTest(TestCase):
    def _provider(self, session, *, supports_reference: bool = True):
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "secret-test-key",
                "OPENROUTER_HTTP_REFERER": "https://craft.example",
                "OPENROUTER_APP_TITLE": "Craft",
            },
            clear=False,
        ):
            return OpenRouterImagesProvider(
                _dynamic_spec(supports_reference=supports_reference),
                session=session,
            )

    def test_generate_sends_only_supported_whitelisted_parameters(self):
        session = _session(
            post_response=_response(
                200,
                {
                    "data": [{"b64_json": PNG_B64}],
                    "usage": {"cost": 0.040125, "prompt_tokens": 250},
                },
            )
        )
        provider = self._provider(session)

        images = provider.generate(
            "  a cat  ",
            aspect_ratio="1:1",
            variant_count=1,
            resolution="1K",
            quality="high",
            output_format="png",
            seed=42,
            arbitrary_frontend_value="must-not-pass",
            extra_body={"provider": {"only": ["attacker"]}},
        )

        self.assertEqual(images, [NORMALIZED_PNG_BYTES])
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "openai/gpt-image-2")
        self.assertEqual(payload["prompt"], "a cat")
        self.assertNotIn("n", payload)
        self.assertNotIn("stream", payload)
        self.assertEqual(payload["aspect_ratio"], "1:1")
        self.assertNotIn("arbitrary_frontend_value", payload)
        self.assertNotIn("extra_body", payload)
        self.assertEqual(
            session.headers["Authorization"],
            "Bearer secret-test-key",
        )
        self.assertEqual(session.headers["HTTP-Referer"], "https://craft.example")
        self.assertEqual(session.headers["X-OpenRouter-Title"], "Craft")
        self.assertEqual(provider.usage_snapshot()["costUsd"], "0.040125")
        self.assertEqual(provider.usage_snapshot()["promptTokens"], 250)

    def test_generate_sends_n_only_for_multiple_images(self):
        session = _session(
            post_response=_response(
                200,
                {"data": [{"b64_json": PNG_B64}, {"b64_json": PNG_B64}]},
            )
        )
        provider = self._provider(session)

        images = provider.generate("two cats", variant_count=2)

        self.assertEqual(len(images), 2)
        self.assertEqual(session.post.call_args.kwargs["json"]["n"], 2)

    def test_generate_accepts_data_url_in_b64_field(self):
        session = _session(
            post_response=_response(
                200,
                {"data": [{"b64_json": f"data:image/png;base64,{PNG_B64}"}]},
            )
        )
        provider = self._provider(session)
        self.assertEqual(provider.generate("cat"), [NORMALIZED_PNG_BYTES])

    def test_generate_rejects_conflicting_explicit_size(self):
        session = _session()
        provider = self._provider(session)
        with self.assertRaises(ImageProviderError) as captured:
            provider.generate("cat", aspect_ratio="1:1", size="1024x1024")
        self.assertEqual(captured.exception.http_status, 400)
        session.post.assert_not_called()

    def test_generate_rejects_out_of_range_compression(self):
        session = _session()
        provider = self._provider(session)
        with self.assertRaises(ImageProviderError) as captured:
            provider.generate("cat", output_compression=101)
        self.assertEqual(captured.exception.code, CODE_BAD_RESPONSE)
        session.post.assert_not_called()

    def test_reference_is_sent_as_data_url_object(self):
        session = _session(
            post_response=_response(200, {"data": [{"b64_json": PNG_B64}]})
        )
        provider = self._provider(session)
        images = provider.generate_with_reference("restyle", PNG_BYTES)

        self.assertEqual(images, [NORMALIZED_PNG_BYTES])
        payload = session.post.call_args.kwargs["json"]
        self.assertNotIn("n", payload)
        self.assertNotIn("stream", payload)
        reference = payload["input_references"][0]
        self.assertEqual(reference["type"], "image_url")
        self.assertTrue(reference["image_url"]["url"].startswith(
            "data:image/png;base64,"
        ))

    def test_error_statuses_are_mapped_without_raw_body(self):
        cases = {
            400: (CODE_BAD_RESPONSE, 400),
            401: (CODE_FORBIDDEN, 502),
            402: (CODE_FORBIDDEN, 502),
            403: (CODE_FORBIDDEN, 502),
            413: (CODE_BAD_RESPONSE, 413),
            422: (CODE_BAD_RESPONSE, 400),
            429: (CODE_UNAVAILABLE, 503),
            502: (CODE_UNAVAILABLE, 503),
            503: (CODE_UNAVAILABLE, 503),
            504: (CODE_UNAVAILABLE, 504),
        }
        for provider_status, expected in cases.items():
            with self.subTest(provider_status=provider_status):
                session = _session(
                    post_response=_response(
                        provider_status,
                        {"error": {"message": "sensitive body"}},
                        text="sensitive body",
                    )
                )
                provider = self._provider(session)
                with self.assertRaises(ImageProviderError) as captured:
                    provider.generate("cat")
                self.assertEqual(
                    (captured.exception.code, captured.exception.http_status),
                    expected,
                )
                self.assertIsNone(captured.exception.provider_body)
                self.assertEqual(captured.exception.provider_body_length, 14)

    def test_content_policy_error_is_classified_without_raw_body(self):
        session = _session(
            post_response=_response(
                400,
                {
                    "error": {
                        "message": "sensitive moderation detail",
                        "metadata": {
                            "error_type": "content_policy_violation",
                            "provider_code": "safety_block",
                        },
                    }
                },
                text="sensitive moderation detail",
            )
        )
        provider = self._provider(session)

        with (
            self.assertLogs(
                "w_craft_back.services.image_generation.openrouter_images",
                level="WARNING",
            ) as logs,
            self.assertRaises(ImageProviderError) as captured,
        ):
            provider.generate("cat")

        self.assertEqual(captured.exception.code, CODE_BLOCKED)
        output = " ".join(logs.output)
        self.assertIn("error_type=content_policy_violation", output)
        self.assertIn("provider_code=safety_block", output)
        self.assertNotIn("sensitive moderation detail", output)
