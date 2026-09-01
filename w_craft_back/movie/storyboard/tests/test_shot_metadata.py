from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.test import override_settings

from w_craft_back.movie.storyboard.errors import StoryboardError
from w_craft_back.movie.storyboard.shot_list import AIShotMetadataService


class StubProvider:
    model = "stub/light-text"

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def suggest(self, *, prompt: str, schema: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append({"prompt": prompt, "schema": schema})
        return self.payload


class SequenceProvider(StubProvider):
    def __init__(self, payloads: list[Mapping[str, Any]]) -> None:
        super().__init__({})
        self.payloads = payloads

    def suggest(self, *, prompt: str, schema: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append({"prompt": prompt, "schema": schema})
        return self.payloads[len(self.calls) - 1]


class ErrorProvider(StubProvider):
    def __init__(self, error: StoryboardError) -> None:
        super().__init__({})
        self.error = error

    def suggest(self, *, prompt: str, schema: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append({"prompt": prompt, "schema": schema})
        raise self.error


class ShotMetadataServiceTests(SimpleTestCase):
    def test_title_uses_only_the_verified_selection_and_strict_schema(self):
        provider = StubProvider({"value": "  Анна входит  "})
        scene_text = "Анна входит. За окном начинается дождь."

        result = AIShotMetadataService(provider=provider).suggest(
            field="title",
            scene_title="Кухня",
            scene_text=scene_text,
            scene_version=3,
            expected_scene_version=3,
            source_start=0,
            source_end=12,
            language="ru",
        )

        self.assertEqual(result, {"field": "title", "value": "Анна входит"})
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        self.assertIn("Анна входит", call["prompt"])
        self.assertNotIn("начинается дождь", call["prompt"])
        self.assertEqual(
            call["schema"]["properties"]["value"]["type"],
            "string",
        )
        self.assertNotIn("minLength", call["schema"]["properties"]["value"])
        self.assertNotIn("maxLength", call["schema"]["properties"]["value"])
        self.assertFalse(call["schema"]["additionalProperties"])

    def test_description_has_a_small_output_budget(self):
        provider = StubProvider({"value": "Анна входит в кухню."})
        scene_text = "Анна входит в кухню."

        result = AIShotMetadataService(provider=provider).suggest(
            field="description",
            scene_title="Кухня",
            scene_text=scene_text,
            scene_version=1,
            expected_scene_version=1,
            source_start=0,
            source_end=len(scene_text),
            language="ru",
        )

        self.assertEqual(result["field"], "description")
        self.assertIn(
            "1000 characters",
            provider.calls[0]["schema"]["properties"]["value"]["description"],
        )

    @override_settings(
        STORYBOARD_SHOT_METADATA_MODEL="openrouter/google/gemma-4-26b-a4b-it:free",
        STORYBOARD_SHOT_METADATA_MODELS="openrouter/google/gemma-4-26b-a4b-it:free",
        STORYBOARD_SHOT_LIST_MODELS="gemini/gemini-2.5-flash",
        OPENROUTER_API_KEY="test-key",
    )
    def test_default_provider_uses_the_dedicated_low_cost_route(self):
        completion = Mock(return_value=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content='{"value": "draft"}')),
        ]))
        litellm = SimpleNamespace(completion=completion)
        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=litellm,
        ):
            service = AIShotMetadataService()
            service.provider.suggest(prompt="scene", schema={"type": "object"})

        self.assertEqual(
            service.provider.model,
            "openrouter/google/gemma-4-26b-a4b-it:free",
        )
        self.assertEqual(service.provider.max_tokens, 512)
        self.assertEqual(service.provider.response_format, "json_object")
        request = completion.call_args.kwargs
        self.assertEqual(request["max_tokens"], 512)
        self.assertNotIn("reasoning", request["extra_body"])
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["extra_body"]["provider"], {
            "require_parameters": True,
            "allow_fallbacks": False,
        })

    def test_falls_back_to_the_next_free_provider_after_rate_limit(self):
        limited = ErrorProvider(StoryboardError(
            "rate limited",
            code="STORYBOARD_AI_RATE_LIMITED",
            http_status=502,
            retryable=True,
            upstream_status=429,
        ))
        available = StubProvider({"value": "Анна входит"})
        scene_text = "Анна входит в кухню."

        result = AIShotMetadataService(providers=(limited, available)).suggest(
            field="title",
            scene_title="Кухня",
            scene_text=scene_text,
            scene_version=1,
            expected_scene_version=1,
            source_start=0,
            source_end=len(scene_text),
            language="ru",
        )

        self.assertEqual(result["value"], "Анна входит")
        self.assertEqual(len(limited.calls), 1)
        self.assertEqual(len(available.calls), 1)

    def test_does_not_hide_a_bad_request_with_provider_fallback(self):
        rejected = ErrorProvider(StoryboardError(
            "bad request",
            code="STORYBOARD_AI_PROVIDER_REJECTED",
            http_status=502,
            retryable=False,
            upstream_status=400,
        ))
        unused = StubProvider({"value": "Unused"})

        with self.assertRaises(StoryboardError) as raised:
            AIShotMetadataService(providers=(rejected, unused)).suggest(
                field="description",
                scene_title="Кухня",
                scene_text="Анна входит.",
                scene_version=1,
                expected_scene_version=1,
                source_start=0,
                source_end=5,
                language="ru",
            )

        self.assertEqual(raised.exception.upstream_status, 400)
        self.assertEqual(unused.calls, [])

    def test_retries_one_malformed_free_model_response(self):
        provider = SequenceProvider([
            {"unexpected": "draft"},
            {"value": "Анна входит в кухню."},
        ])
        scene_text = "Анна входит в кухню."

        result = AIShotMetadataService(provider=provider).suggest(
            field="description",
            scene_title="Кухня",
            scene_text=scene_text,
            scene_version=1,
            expected_scene_version=1,
            source_start=0,
            source_end=len(scene_text),
            language="ru",
        )

        self.assertEqual(result["value"], "Анна входит в кухню.")
        self.assertEqual(len(provider.calls), 2)

    def test_stale_scene_is_rejected_before_provider_call(self):
        provider = StubProvider({"value": "Unused"})

        with self.assertRaises(StoryboardError) as raised:
            AIShotMetadataService(provider=provider).suggest(
                field="title",
                scene_title="Kitchen",
                scene_text="Anna enters.",
                scene_version=4,
                expected_scene_version=3,
                source_start=0,
                source_end=5,
            )

        self.assertEqual(raised.exception.code, "STORYBOARD_SOURCE_STALE")
        self.assertEqual(raised.exception.http_status, 409)
        self.assertEqual(provider.calls, [])

    def test_invalid_or_empty_range_is_rejected_before_provider_call(self):
        for source_start, source_end, expected_code in (
            (0, 20, "STORYBOARD_SOURCE_RANGE_INVALID"),
            (4, 7, "STORYBOARD_SOURCE_RANGE_EMPTY"),
        ):
            with self.subTest(source_start=source_start, source_end=source_end):
                provider = StubProvider({"value": "Unused"})
                with self.assertRaises(StoryboardError) as raised:
                    AIShotMetadataService(provider=provider).suggest(
                        field="title",
                        scene_title="Kitchen",
                        scene_text="Anna   enters.",
                        scene_version=1,
                        expected_scene_version=1,
                        source_start=source_start,
                        source_end=source_end,
                    )
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(provider.calls, [])

    def test_invalid_provider_value_is_not_returned(self):
        provider = StubProvider({"value": "", "unexpected": True})

        with self.assertRaises(StoryboardError) as raised:
            AIShotMetadataService(provider=provider).suggest(
                field="title",
                scene_title="Kitchen",
                scene_text="Anna enters.",
                scene_version=1,
                expected_scene_version=1,
                source_start=0,
                source_end=5,
            )

        self.assertEqual(raised.exception.code, "STORYBOARD_AI_BAD_RESPONSE")
