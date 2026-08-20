from __future__ import annotations

from decimal import Decimal
import json
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from w_craft_back.movie.music.providers import MusicProviderError
from w_craft_back.movie.music.providers.elevenlabs_music import (
    ElevenLabsMusicProvider,
)
from w_craft_back.movie.music.providers.minimax_music import MiniMaxMusicProvider
from w_craft_back.movie.music.providers.model_registry import (
    pricing_from_snapshot,
    public_audio_model_catalog,
    resolve_audio_model,
)


class RecordingContext:
    def __init__(self) -> None:
        self.checkpoints = 0

    def heartbeat(self) -> None:
        self.checkpoints += 1

    def is_cancelled(self) -> bool:
        return False

    def checkpoint(self) -> None:
        self.checkpoints += 1


def _response(
    status_code: int,
    *,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.iter_content.return_value = [content] if content else []
    return response


def _instrumental_request(duration: int = 30) -> dict[str, object]:
    return {
        "contentMode": "instrumental",
        "durationSeconds": duration,
        "variantCount": 1,
        "positivePrompt": "cinematic strings",
        "negativePrompt": "harsh brass",
    }


def _song_request(duration: int = 60) -> dict[str, object]:
    return {
        "contentMode": "song",
        "durationSeconds": duration,
        "variantCount": 1,
        "positivePrompt": "intimate electronic ballad",
        "negativePrompt": "distorted drums",
        "lyricsLanguage": "ru",
        "vocalStyle": {"timbre": "warm"},
        "lyricsSections": [
            {"type": "verse", "label": "Первый", "text": "Первая строка"},
            {"type": "chorus", "label": "Припев", "text": "Свет внутри"},
        ],
    }


@override_settings(
    MUSIC_ELEVENLABS_TIMEOUT_SECONDS=120,
    MUSIC_ELEVENLABS_RESPONSE_DEADLINE_SECONDS=300,
    MUSIC_ELEVENLABS_COST_USD_PER_MINUTE="0.15",
    MUSIC_MINIMAX_TIMEOUT_SECONDS=121,
    MUSIC_MINIMAX_RESPONSE_DEADLINE_SECONDS=300,
    MUSIC_MINIMAX_COST_USD_PER_GENERATION="0.15",
    MUSIC_MAX_OUTPUT_BYTES=1024,
)
class CommercialMusicProviderTests(SimpleTestCase):
    def elevenlabs(
        self,
        session: MagicMock | None = None,
    ) -> ElevenLabsMusicProvider:
        return ElevenLabsMusicProvider(
            model_name="music_v2",
            session=session or MagicMock(),
            api_key="eleven-key",
            base_url="https://eleven.test",
        )

    def minimax(
        self,
        session: MagicMock | None = None,
    ) -> MiniMaxMusicProvider:
        return MiniMaxMusicProvider(
            model_name="music-3.0",
            session=session or MagicMock(),
            api_key="minimax-key",
            base_url="https://minimax.test",
            legacy_access_confirmed=True,
        )

    def test_capabilities_and_provider_native_pricing(self):
        eleven = self.elevenlabs()
        minimax = self.minimax()

        self.assertEqual(eleven.capabilities().max_duration_seconds, 300)
        self.assertFalse(eleven.capabilities().supports_seed)
        self.assertEqual(
            eleven.pricing(1, duration_seconds=30).estimated_cost,
            Decimal("0.075"),
        )
        self.assertEqual(minimax.capabilities().max_lyrics_chars, 3500)
        self.assertEqual(minimax.pricing(1).estimated_cost, Decimal("0.15"))

    def test_model_registry_prices_duration_and_preserves_snapshot(self):
        with override_settings(ELEVENLABS_API_KEY="configured"):
            resolved = resolve_audio_model("elevenlabs-music-v2")
            snapshot = resolved.snapshot(1, duration_seconds=90)

        self.assertEqual(snapshot["estimatedCostUsd"], "0.225")
        self.assertEqual(snapshot["pricing"]["billingUnit"], "minute")
        self.assertEqual(snapshot["pricing"]["durationSeconds"], 90)
        with override_settings(MUSIC_ELEVENLABS_COST_USD_PER_MINUTE="9"):
            restored = pricing_from_snapshot(snapshot)
        self.assertEqual(restored.estimated_cost, Decimal("0.225"))

    def test_minimax_catalog_requires_legacy_confirmation(self):
        with override_settings(
            MINIMAX_API_KEY="configured",
            MUSIC_MINIMAX_LEGACY_PAID_ACCESS_CONFIRMED=False,
        ):
            row = next(
                item
                for item in public_audio_model_catalog()
                if item["key"] == "minimax-music-3"
            )
            self.assertFalse(row["configured"])
            with self.assertRaises(MusicProviderError):
                resolve_audio_model("minimax-music-3")

        with override_settings(
            MINIMAX_API_KEY="configured",
            MUSIC_MINIMAX_LEGACY_PAID_ACCESS_CONFIRMED=True,
        ):
            self.assertEqual(
                resolve_audio_model("minimax-music-3").route.backend_name,
                "minimax-music-3",
            )

    def test_minimax_constructor_requires_explicit_legacy_gate(self):
        with override_settings(
            MINIMAX_API_KEY="key",
            MUSIC_MINIMAX_LEGACY_PAID_ACCESS_CONFIRMED=False,
        ):
            with self.assertRaises(MusicProviderError) as raised:
                MiniMaxMusicProvider(model_name="music-3.0")
        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_NOT_CONFIGURED")

    def test_elevenlabs_instrumental_success_streams_bounded_raw_audio(self):
        response = _response(
            200,
            content=b"ID3eleven",
            headers={"song-id": "song-123"},
        )
        session = MagicMock()
        session.post.return_value = response
        context = RecordingContext()

        result = self.elevenlabs(session).submit(
            _instrumental_request(),
            context,
        )

        output = result.outputs[0]
        self.assertEqual(output.payload, b"ID3eleven")
        self.assertEqual(output.provider_request_id, "song-123")
        self.assertIsNone(output.duration_seconds)
        call = session.post.call_args
        self.assertEqual(call.args[0], "https://eleven.test/v1/music")
        self.assertEqual(
            call.kwargs["params"]["output_format"],
            "mp3_48000_192",
        )
        self.assertEqual(call.kwargs["headers"]["xi-api-key"], "eleven-key")
        self.assertEqual(call.kwargs["json"]["model_id"], "music_v2")
        self.assertEqual(call.kwargs["json"]["music_length_ms"], 30_000)
        self.assertTrue(call.kwargs["json"]["force_instrumental"])
        self.assertNotIn("seed", call.kwargs["json"])
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(call.kwargs["stream"])
        self.assertEqual(call.kwargs["timeout"], 120)
        self.assertGreaterEqual(context.checkpoints, 2)
        response.close.assert_called()

    def test_elevenlabs_song_uses_ordered_music_v2_composition_plan(self):
        response = _response(200, content=b"ID3song")
        session = MagicMock()
        session.post.return_value = response

        self.elevenlabs(session).submit(_song_request(), RecordingContext())

        body = session.post.call_args.kwargs["json"]
        self.assertNotIn("prompt", body)
        chunks = body["composition_plan"]["chunks"]
        self.assertEqual(sum(chunk["duration_ms"] for chunk in chunks), 60_000)
        self.assertIn("[Verse]\nПервая строка", chunks[0]["text"])
        self.assertIn("[Chorus]\nСвет внутри", chunks[1]["text"])
        self.assertEqual(chunks[0]["context_adherence"], "high")

    def test_elevenlabs_long_song_splits_chunks_with_valid_durations(self):
        request = _song_request(600)
        request["lyricsSections"] = [
            {"type": "verse", "text": "Only user lyric"}
        ]
        response = _response(200, content=b"ID3song")
        session = MagicMock()
        session.post.return_value = response

        self.elevenlabs(session).submit(request, RecordingContext())

        chunks = session.post.call_args.kwargs["json"]["composition_plan"][
            "chunks"
        ]
        self.assertEqual(len(chunks), 5)
        self.assertTrue(all(chunk["duration_ms"] == 120_000 for chunk in chunks))
        self.assertEqual(chunks[1]["text"], "[Instrumental]")

    def test_minimax_song_success_parses_hex_and_safe_metadata(self):
        audio = b"ID3minimax"
        provider_payload = {
            "data": {"audio": audio.hex(), "status": 2},
            "trace_id": "trace-123",
            "extra_info": {"music_duration": 31_500},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        response = _response(200, content=json.dumps(provider_payload).encode())
        session = MagicMock()
        session.post.return_value = response

        result = self.minimax(session).submit(_song_request(), RecordingContext())

        output = result.outputs[0]
        self.assertEqual(output.payload, audio)
        self.assertEqual(output.provider_request_id, "trace-123")
        self.assertEqual(output.duration_seconds, 31.5)
        call = session.post.call_args
        self.assertEqual(call.args[0], "https://minimax.test/v1/music_generation")
        self.assertEqual(call.kwargs["json"]["model"], "music-3.0")
        self.assertEqual(call.kwargs["json"]["output_format"], "hex")
        self.assertFalse(call.kwargs["json"]["is_instrumental"])
        self.assertIn("[Verse]", call.kwargs["json"]["lyrics"])
        self.assertNotIn("lyricsSections", repr(call.kwargs["json"]))
        self.assertFalse(call.kwargs["allow_redirects"])
        response.close.assert_called()

    def test_minimax_instrumental_omits_lyrics(self):
        response = _response(
            200,
            content=json.dumps(
                {
                    "data": {"audio": b"ID3".hex(), "status": 2},
                    "base_resp": {"status_code": 0},
                }
            ).encode(),
        )
        session = MagicMock()
        session.post.return_value = response

        self.minimax(session).submit(_instrumental_request(), RecordingContext())

        body = session.post.call_args.kwargs["json"]
        self.assertTrue(body["is_instrumental"])
        self.assertNotIn("lyrics", body)

    def test_invalid_hex_is_unknown_and_captures_confirmed_cost(self):
        response = _response(
            200,
            content=json.dumps(
                {
                    "data": {"audio": "not-hex", "status": 2},
                    "base_resp": {"status_code": 0},
                }
            ).encode(),
        )
        session = MagicMock()
        session.post.return_value = response

        with self.assertRaises(MusicProviderError) as raised:
            self.minimax(session).submit(
                _instrumental_request(),
                RecordingContext(),
            )

        self.assertTrue(raised.exception.outcome_unknown)
        self.assertTrue(raised.exception.cost_incurred)
        self.assertFalse(raised.exception.retryable)

    def test_invalid_minimax_duration_is_terminal_and_billable(self):
        response = _response(
            200,
            content=json.dumps(
                {
                    "data": {"audio": b"ID3".hex(), "status": 2},
                    "extra_info": {"music_duration": "not-a-number"},
                    "base_resp": {"status_code": 0},
                }
            ).encode(),
        )
        session = MagicMock()
        session.post.return_value = response

        with self.assertRaises(MusicProviderError) as raised:
            self.minimax(session).submit(
                _instrumental_request(),
                RecordingContext(),
            )

        self.assertTrue(raised.exception.outcome_unknown)
        self.assertTrue(raised.exception.cost_incurred)
        self.assertFalse(raised.exception.retryable)

    def test_minimax_duration_is_sent_as_approximate_prompt_instruction(self):
        response = _response(
            200,
            content=json.dumps(
                {
                    "data": {"audio": b"ID3".hex(), "status": 2},
                    "base_resp": {"status_code": 0},
                }
            ).encode(),
        )
        session = MagicMock()
        session.post.return_value = response

        self.minimax(session).submit(_instrumental_request(), RecordingContext())

        prompt = session.post.call_args.kwargs["json"]["prompt"]
        self.assertIn("Target duration: approximately 30 seconds.", prompt)

    @override_settings(MUSIC_MAX_OUTPUT_BYTES=4)
    def test_both_providers_reject_oversized_confirmed_output(self):
        minimax_payload = {
            "data": {"audio": b"ID3audio".hex(), "status": 2},
            "base_resp": {"status_code": 0},
        }
        cases = (
            (self.elevenlabs, _response(200, content=b"ID3audio")),
            (
                self.minimax,
                _response(200, content=json.dumps(minimax_payload).encode()),
            ),
        )
        for provider_factory, response in cases:
            with self.subTest(provider=provider_factory.__name__):
                session = MagicMock()
                session.post.return_value = response
                with self.assertRaises(MusicProviderError) as raised:
                    provider_factory(session).submit(
                        _instrumental_request(),
                        RecordingContext(),
                    )
                self.assertEqual(raised.exception.code, "MUSIC_OUTPUT_TOO_LARGE")
                self.assertTrue(raised.exception.cost_incurred)

    def test_response_deadline_after_acceptance_is_unknown(self):
        cases = (
            (self.elevenlabs, _response(200, content=b"ID3audio")),
            (
                self.minimax,
                _response(
                    200,
                    content=json.dumps(
                        {
                            "data": {"audio": b"ID3".hex(), "status": 2},
                            "base_resp": {"status_code": 0},
                        }
                    ).encode(),
                ),
            ),
        )
        for provider_factory, response in cases:
            with self.subTest(provider=provider_factory.__name__):
                session = MagicMock()
                session.post.return_value = response
                module = (
                    "w_craft_back.movie.music.providers."
                    "elevenlabs_music.time.monotonic"
                    if provider_factory == self.elevenlabs
                    else "w_craft_back.movie.music.providers."
                    "minimax_music.time.monotonic"
                )
                with patch(module, side_effect=[0, 301]):
                    with self.assertRaises(MusicProviderError) as raised:
                        provider_factory(session).submit(
                            _instrumental_request(),
                            RecordingContext(),
                        )
                self.assertTrue(raised.exception.outcome_unknown)
                self.assertTrue(raised.exception.cost_incurred)

    def test_http_statuses_do_not_blindly_retry_unknown_submissions(self):
        expected = {
            302: ("MUSIC_PROVIDER_REJECTED", False, False),
            400: ("MUSIC_PROVIDER_REJECTED", False, False),
            429: ("MUSIC_PROVIDER_RATE_LIMITED", True, False),
            503: ("MUSIC_PROVIDER_OUTCOME_UNKNOWN", False, True),
        }
        for provider_factory in (self.elevenlabs, self.minimax):
            for status, (code, retryable, unknown) in expected.items():
                with self.subTest(provider=provider_factory.__name__, status=status):
                    session = MagicMock()
                    session.post.return_value = _response(status)
                    with self.assertRaises(MusicProviderError) as raised:
                        provider_factory(session).submit(
                            _instrumental_request(),
                            RecordingContext(),
                        )
                    self.assertEqual(raised.exception.code, code)
                    self.assertEqual(raised.exception.retryable, retryable)
                    self.assertEqual(raised.exception.outcome_unknown, unknown)

    def test_network_timeout_after_post_has_unknown_outcome(self):
        for provider_factory in (self.elevenlabs, self.minimax):
            with self.subTest(provider=provider_factory.__name__):
                session = MagicMock()
                session.post.side_effect = requests.Timeout("late timeout")
                with self.assertRaises(MusicProviderError) as raised:
                    provider_factory(session).submit(
                        _instrumental_request(),
                        RecordingContext(),
                    )
                self.assertTrue(raised.exception.outcome_unknown)
                self.assertFalse(raised.exception.retryable)

    def test_nonofficial_configured_origins_and_models_are_rejected(self):
        with override_settings(
            ELEVENLABS_API_KEY="key",
            MUSIC_ELEVENLABS_API_BASE_URL="https://evil.example",
        ):
            with self.assertRaises(MusicProviderError):
                ElevenLabsMusicProvider(model_name="music_v2")
        with self.assertRaises(MusicProviderError):
            self.elevenlabs().__class__(
                model_name="music_v1",
                api_key="key",
            )
