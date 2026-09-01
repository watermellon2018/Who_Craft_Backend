"""Shot provenance stays exact, budgeted, and independent of model quotations."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from django.test import SimpleTestCase

from w_craft_back.movie.storyboard.errors import StoryboardError
from w_craft_back.movie.storyboard.shot_list import AIShotListService
from w_craft_back.movie.storyboard.source import (
    SOURCE_TEXT_BUDGET,
    ShotListSource,
    build_source_snapshot,
    prompt_source_segments,
    source_from_scene,
)


class ShotListSourceTests(SimpleTestCase):
    def source(self, text: str) -> ShotListSource:
        return build_source_snapshot(scene_id=42, scene_version=3, text=text)

    def test_language_is_explicit_in_prompt_and_schema_without_translating_source(self):
        source = self.source('Анна говорит: «Hello!»')
        for language, target in (("ru", "Russian"), ("en", "English")):
            with self.subTest(language=language):
                provider = SimpleNamespace(suggest=Mock(return_value={
                    "shots": [self.shot([source["segments"][0]["id"]])],
                }))
                result = AIShotListService(provider=provider).suggest(
                    context={}, max_shots=1, source=source, language=language,
                )
                request = provider.suggest.call_args.kwargs
                self.assertIn(
                    f"Write every title and description in {target} ({language})",
                    request["prompt"],
                )
                fields = request["schema"]["properties"]["shots"]["items"][
                    "properties"
                ]
                self.assertIn(target, fields["title"]["description"])
                self.assertIn(target, fields["description"]["description"])
                self.assertEqual(result["source"], source)
                provider.suggest.assert_called_once()

    def test_snapshot_preserves_unicode_whitespace_and_repeated_sentences(self) -> None:
        text = '  Он сказал: «Привет!»  Она ответила: «Привет!»\r\n\r\nУход. Уход.\t'
        source = self.source(text)
        self.assertEqual("".join(item["text"] for item in source["segments"]), text)
        self.assertEqual(len({item["id"] for item in source["segments"]}),
                         len(source["segments"]))
        self.assertEqual(
            source["content_hash"], hashlib.sha256(text.encode()).hexdigest(),
        )
        self.assertEqual(source, self.source(text))
        self.assertNotEqual(
            source["content_hash"], self.source(text + " ")["content_hash"],
        )
        self.assertEqual(source["scene_id"], 42)
        self.assertEqual(source["scene_version"], 3)
        self.assertFalse(source["truncated"])

    def test_source_uses_existing_canonical_text_precedence(self) -> None:
        scene = SimpleNamespace(
            pk=42, version=3, script_text="  Canonical.\nFull script.  ",
            script_blocks=[{"text": "Different block text"}],
            description="Description", notes="Notes",
        )
        source = source_from_scene(scene)
        self.assertEqual(
            "".join(item["text"] for item in source["segments"]),
            "Canonical.\nFull script.",
        )
        scene.script_text = ""
        self.assertEqual(
            "".join(item["text"] for item in source_from_scene(scene)["segments"]),
            "Different block text",
        )

    def test_long_source_returns_full_text_and_budgets_the_prompt(self) -> None:
        text = "Начало. " + "я" * SOURCE_TEXT_BUDGET + " Never sent."
        source = self.source(text)
        self.assertTrue(source["truncated"])
        self.assertEqual("".join(item["text"] for item in source["segments"]), text)
        self.assertEqual(
            "".join(item["text"] for item in prompt_source_segments(source)),
            text[:SOURCE_TEXT_BUDGET],
        )
        for size in (0, SOURCE_TEXT_BUDGET):
            with self.subTest(size=size):
                exact = self.source("x" * size)
                self.assertFalse(exact["truncated"])
                self.assertEqual(prompt_source_segments(exact), exact["segments"])

    def test_model_sees_source_once_and_cannot_replace_snapshot(self) -> None:
        source = self.source("Exact unique quotation.\nAnother action.")
        before = deepcopy(source)
        context = {"scene": {"id": 42, "title": "A scene", "text": "Old body copy"}}
        source_id = source["segments"][0]["id"]
        provider = SimpleNamespace(suggest=Mock(return_value={
            "shots": [self.shot([source_id]), self.shot([source_id])],
            "source": {"segments": [{"text": "Invented quotation"}]},
        }))
        result = AIShotListService(provider=provider).suggest(
            context=context, max_shots=3, source=source,
        )
        self.assertEqual(result["source"], before)
        self.assertEqual(source, before)
        prompt = provider.suggest.call_args.kwargs["prompt"]
        self.assertEqual(prompt.count("Exact unique quotation."), 1)
        self.assertNotIn("Old body copy", prompt)
        self.assertNotIn("Invented quotation", prompt)
        metadata = json.loads(prompt.split("Scene metadata: ", 1)[1])
        self.assertEqual(metadata["scene"]["source_segments"], source["segments"])
        self.assertNotIn("text", metadata["scene"])
        self.assertEqual(context["scene"]["text"], "Old body copy")
        fields = provider.suggest.call_args.kwargs["schema"][
            "properties"]["shots"]["items"]
        self.assertIn("source_segment_ids", fields["required"])
        self.assertNotIn("uniqueItems", fields["properties"]["source_segment_ids"])
        self.assertEqual(fields["properties"]["source_segment_ids"]["minItems"], 1)
        self.assertEqual(
            fields["properties"]["source_segment_ids"]["items"]["enum"],
            sorted(item["id"] for item in source["segments"]),
        )

    @staticmethod
    def shot(source_ids: list[str] | None) -> dict[str, Any]:
        return {
            "title": "Reaction", "description": "A reaction shot.",
            "source_segment_ids": source_ids,
            "suggested_characters": [], "suggested_location": None,
            "suggested_assets": [], "suggested_framing": "close",
        }

    def test_unknown_duplicate_missing_and_unseen_source_ids_are_rejected(self) -> None:
        source = self.source("x" * SOURCE_TEXT_BUDGET + " Secret unseen text.")
        valid_id, unseen_id = [item["id"] for item in source["segments"]]
        for ids in (
            ["private-invented-id"], [valid_id, valid_id], [], [unseen_id], None,
        ):
            with self.subTest(ids=ids):
                provider = SimpleNamespace(suggest=Mock(return_value={
                    "shots": [self.shot(ids)],
                }))
                with self.assertLogs(
                    "w_craft_back.movie.storyboard.shot_list", level="WARNING",
                ) as logs:
                    with self.assertRaises(StoryboardError) as captured:
                        AIShotListService(provider=provider).suggest(
                            context={}, max_shots=3, source=source,
                        )
                self.assertEqual(captured.exception.code, "STORYBOARD_AI_BAD_RESPONSE")
                self.assertNotIn("private-invented-id", "".join(logs.output))
                schema = provider.suggest.call_args.kwargs["schema"]
                self.assertEqual(
                    schema["properties"]["shots"]["items"]["properties"][
                        "source_segment_ids"]["items"]["enum"],
                    [valid_id],
                )
                self.assertNotIn(
                    "Secret unseen text", provider.suggest.call_args.kwargs["prompt"],
                )

    def test_omitting_source_ids_is_invalid_even_for_a_custom_provider(self) -> None:
        shot = self.shot([])
        del shot["source_segment_ids"]
        provider = SimpleNamespace(suggest=Mock(return_value={"shots": [shot]}))
        with self.assertRaises(StoryboardError):
            AIShotListService(provider=provider).suggest(
                context={}, max_shots=3, source=self.source("Original."),
            )
