"""Provider failures remain actionable without leaking screenplay or secrets."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from w_craft_back.movie.storyboard.errors import StoryboardError
from w_craft_back.movie.storyboard.shot_list import (
    AIShotListService,
    LiteLLMShotListProvider,
    SHOT_LIST_SCHEMA,
)
from w_craft_back.movie.storyboard.source import build_source_snapshot
from w_craft_back.observability import JsonLogFormatter


MODEL = "openrouter/qwen/qwen3-235b-a22b-2507"
LOGGER = "w_craft_back.movie.storyboard.shot_list"
CONTEXT = {
    "scene": {"text": "Private scene"},
    "characters": [{"id": "dog", "name": "Alice"}],
    "locations": [{"id": "pier", "name": "Pier"}],
    "visualAssets": [],
}
SOURCE = build_source_snapshot(
    scene_id=1, scene_version=1, text=CONTEXT["scene"]["text"],
)
SHOT = {
    "title": "Arrival",
    "description": "The character arrives at the pier.",
    "source_segment_ids": [SOURCE["segments"][0]["id"]],
    "suggested_characters": ["dog"],
    "suggested_location": "pier",
    "suggested_assets": [],
    "suggested_framing": "wide",
}


@override_settings(
    OPENROUTER_API_KEY="unused-test-key",
    STORYBOARD_SHOT_LIST_MODEL=MODEL,
    STORYBOARD_SHOT_LIST_MODELS=MODEL,
)
class ShotListDiagnosticsTests(SimpleTestCase):
    def provider(self, completion):
        with patch(
            f"{LOGGER}._load_litellm",
            return_value=SimpleNamespace(completion=completion),
        ):
            return LiteLLMShotListProvider(model=MODEL)

    def test_upstream_failures_have_safe_codes_and_diagnostic_metadata(self):
        for status, expected_code, reason in (
            (400, "STORYBOARD_AI_PROVIDER_REJECTED", "provider_rejected"),
            (402, "STORYBOARD_AI_PROVIDER_REJECTED", "provider_rejected"),
            (429, "STORYBOARD_AI_RATE_LIMITED", "rate_limited"),
            (504, "STORYBOARD_AI_TIMEOUT", "timeout"),
            (503, "STORYBOARD_AI_FAILED", "provider_error"),
        ):
            with self.subTest(status=status):
                error = RuntimeError("SECRET_API_KEY Private scene raw provider body")
                error.status_code = status
                completion = Mock(side_effect=error)
                provider = self.provider(completion)
                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    with self.assertRaises(StoryboardError) as captured:
                        provider.suggest(prompt="Private scene", schema={})
                self.assertEqual(captured.exception.code, expected_code)
                self.assertEqual(captured.exception.http_status, 502)
                formatted = JsonLogFormatter().format(logs.records[0])
                payload = json.loads(formatted)
                self.assertEqual(payload["model"], MODEL)
                self.assertEqual(payload["status_code"], status)
                self.assertEqual(payload["status"], reason)
                self.assertEqual(payload["exception_type"], "RuntimeError")
                self.assertNotIn("SECRET_API_KEY", formatted)
                self.assertNotIn("Private scene", formatted)
                self.assertNotIn("raw provider body", formatted)
                self.assertNotIn("SECRET_API_KEY", captured.exception.detail)
                completion.assert_called_once()

    def test_invalid_json_truncation_and_refusal_have_distinct_safe_reasons(self):
        for content, finish_reason, refusal, reason in (
            ("Private scene invalid JSON", "stop", None, "invalid_json"),
            ('{"shots": [', "length", None, "response_truncated"),
            (None, "stop", "Private refusal", "response_refused"),
            (None, "content_filter", None, "response_refused"),
            ("[]", "stop", None, "invalid_structure"),
        ):
            with self.subTest(reason=reason):
                completion = Mock(return_value=SimpleNamespace(choices=[
                    SimpleNamespace(
                        finish_reason=finish_reason,
                        message=SimpleNamespace(content=content, refusal=refusal),
                    ),
                ]))
                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    with self.assertRaises(StoryboardError) as captured:
                        self.provider(completion).suggest(prompt="scene", schema={})
                self.assertEqual(captured.exception.code, "STORYBOARD_AI_BAD_RESPONSE")
                self.assertEqual(logs.records[0].status, reason)
                self.assertNotIn("Private", JsonLogFormatter().format(logs.records[0]))

    def test_schema_limits_count_and_references_to_current_scene_without_mutation(self):
        provider = SimpleNamespace(model=MODEL, suggest=Mock(return_value={
            "shots": [deepcopy(SHOT)],
        }))
        service = AIShotListService(provider=provider)
        self.assertEqual(
            service.suggest(context=CONTEXT, max_shots=16, source=SOURCE),
            {"shots": [SHOT], "source": SOURCE},
        )
        first_schema = provider.suggest.call_args.kwargs["schema"]
        self.assertEqual(first_schema["properties"]["shots"]["maxItems"], 16)
        fields = first_schema["properties"]["shots"]["items"]["properties"]
        self.assertEqual(fields["suggested_characters"]["items"]["enum"], ["dog"])
        self.assertEqual(fields["suggested_location"]["enum"], [None, "pier"])
        self.assertEqual(fields["suggested_assets"]["maxItems"], 0)
        self.assertIn(
            "never names or titles", provider.suggest.call_args.kwargs["prompt"],
        )
        provider.suggest.return_value = {"shots": [dict(
            SHOT, suggested_characters=[], suggested_location=None,
            source_segment_ids=[],
        )]}
        service.suggest(
            context={}, max_shots=3,
            source=build_source_snapshot(scene_id=1, scene_version=1, text=""),
        )
        fields = provider.suggest.call_args.kwargs["schema"][
            "properties"]["shots"]["items"]["properties"]
        self.assertEqual(fields["suggested_characters"]["maxItems"], 0)
        self.assertNotIn("enum", fields["suggested_characters"]["items"])
        self.assertEqual(fields["suggested_location"]["enum"], [None])
        self.assertEqual(first_schema["properties"]["shots"]["maxItems"], 16)
        self.assertEqual(SHOT_LIST_SCHEMA["properties"]["shots"]["maxItems"], 40)

    def test_semantic_validation_logs_reason_without_entity_values(self):
        for changes, reason in (
            ({"suggested_characters": ["Alice"]}, "unknown_character"),
            ({"suggested_location": "private-other-location"}, "unknown_location"),
            ({"suggested_assets": ["private-other-asset"]}, "unknown_visual_asset"),
            ({"suggested_characters": None}, "invalid_shot_fields"),
            ({"title": None}, "invalid_shot_fields"),
        ):
            with self.subTest(reason=reason):
                provider = SimpleNamespace(model=MODEL, suggest=Mock(return_value={
                    "shots": [dict(SHOT, **changes)],
                }))
                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    with self.assertRaises(StoryboardError) as captured:
                        AIShotListService(provider=provider).suggest(
                            context=CONTEXT, max_shots=16, source=SOURCE,
                        )
                self.assertEqual(captured.exception.code, "STORYBOARD_AI_BAD_RESPONSE")
                self.assertEqual(logs.records[0].status, reason)
                self.assertNotIn("Alice", JsonLogFormatter().format(logs.records[0]))
                self.assertNotIn(
                    "private-other", JsonLogFormatter().format(logs.records[0]),
                )
