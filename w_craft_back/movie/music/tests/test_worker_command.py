from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from w_craft_back.management.commands.run_generation_worker import Command


class GenerationWorkerQueueTests(SimpleTestCase):
    @patch.object(Command, "_poll_storyboard_jobs", return_value=1)
    @patch.object(Command, "_poll_reference_jobs", return_value=1)
    @patch.object(Command, "_poll_sound_effect_jobs", return_value=1)
    @patch.object(Command, "_poll_music_jobs", return_value=1)
    @patch.object(Command, "_poll_poster_jobs", return_value=1)
    @patch.object(Command, "_poll_character_jobs", return_value=1)
    def test_all_polls_each_queue_once(
        self,
        character,
        poster,
        music,
        sound_effect,
        reference,
        storyboard,
    ):
        output = StringIO()
        call_command("run_generation_worker", once=True, queue="all", stdout=output)
        character.assert_called_once_with(10)
        poster.assert_called_once_with(10)
        music.assert_called_once_with(10)
        sound_effect.assert_called_once_with(10)
        reference.assert_called_once_with(10)
        storyboard.assert_called_once_with(10)
        self.assertIn("Processed 6", output.getvalue())

    @patch.object(Command, "_poll_music_jobs", return_value=1)
    @patch.object(Command, "_poll_poster_jobs", return_value=1)
    @patch.object(Command, "_poll_character_jobs", return_value=1)
    def test_music_selector_does_not_poll_other_queues(self, character, poster, music):
        call_command(
            "run_generation_worker",
            once=True,
            queue="music",
            stdout=StringIO(),
        )
        music.assert_called_once_with(10)
        poster.assert_not_called()
        character.assert_not_called()

    @patch.object(Command, "_poll_reference_jobs", return_value=1)
    @patch.object(Command, "_poll_sound_effect_jobs", return_value=1)
    @patch.object(Command, "_poll_music_jobs", return_value=1)
    def test_sound_effect_selector_is_isolated(self, music, sound_effect, reference):
        call_command(
            "run_generation_worker",
            once=True,
            queue="sound_effect",
            stdout=StringIO(),
        )
        sound_effect.assert_called_once_with(10)
        music.assert_not_called()
        reference.assert_not_called()

    def test_unknown_queue_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("run_generation_worker", once=True, queue="bogus")
