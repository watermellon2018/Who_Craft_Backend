from __future__ import annotations

import io
import wave

from django.test import SimpleTestCase

from w_craft_back.movie.music.prompt_compiler import (
    MusicBriefError,
    compile_music_prompt,
)
from w_craft_back.movie.music.providers.mock import MockAudioProvider

from .helpers import instrumental_brief


class RecordingContext:
    def __init__(self) -> None:
        self.checkpoints = 0

    def heartbeat(self) -> None:
        self.checkpoints += 1

    def is_cancelled(self) -> bool:
        return False

    def checkpoint(self) -> None:
        self.checkpoints += 1


class CompilerProviderTests(SimpleTestCase):
    def test_song_preserves_ordered_lyrics_and_bounds_scene_context(self):
        brief = instrumental_brief()
        brief["content"] = {
            "mode": "song",
            "lyricsLanguage": "ru",
            "vocalStyle": {"timbre": "warm", "delivery": "intimate"},
            "sections": [
                {"type": "verse", "label": "??????", "text": "??????\n??????"},
                {"type": "chorus", "label": "??????", "text": "????? ??? ????"},
            ],
        }
        brief["purpose"] = "song"
        brief["context"] = {"type": "scene", "sceneId": 7}
        compiled = compile_music_prompt(
            brief,
            scene_context={
                "sceneId": 7,
                "title": "Night",
                "durationSeconds": 80,
                "mood": "tense",
                "sceneType": "dialogue",
                "summary": "x" * 700,
                "scriptText": "must never be copied",
            },
            variant_count=2,
        )
        self.assertEqual(compiled["lyricsSections"], brief["content"]["sections"])
        self.assertEqual(len(compiled["sceneContext"]["summary"]), 500)
        self.assertNotIn("scriptText", compiled["sceneContext"])
        self.assertNotIn("must never be copied", compiled["positivePrompt"])

    def test_mock_is_deterministic_distinct_and_playable(self):
        request = compile_music_prompt(instrumental_brief(), variant_count=2)
        request["baseSeed"] = 1234
        context = RecordingContext()
        provider = MockAudioProvider()
        first = provider.submit(request, context)
        second = provider.submit(request, RecordingContext())
        self.assertEqual(first.outputs[0].payload, second.outputs[0].payload)
        self.assertNotEqual(first.outputs[0].payload, first.outputs[1].payload)
        self.assertGreaterEqual(context.checkpoints, 4)
        for output in first.outputs:
            with wave.open(io.BytesIO(output.payload), "rb") as wav_file:
                self.assertEqual(wav_file.getframerate(), 8000)
                self.assertEqual(wav_file.getnframes(), 24000)

    def test_explicit_seed_is_bounded_compiled_and_drives_variants(self):
        brief = instrumental_brief()
        brief["seed"] = 1234
        request = compile_music_prompt(brief, variant_count=2)
        self.assertEqual(request["baseSeed"], 1234)

        outputs = MockAudioProvider().submit(request, RecordingContext()).outputs
        self.assertEqual([output.seed for output in outputs], [1234, 1235])

        for invalid_seed in (-1, 4_294_967_296, True):
            with self.subTest(seed=invalid_seed):
                invalid = instrumental_brief()
                invalid["seed"] = invalid_seed
                with self.assertRaises(MusicBriefError):
                    compile_music_prompt(invalid)
