from __future__ import annotations

import base64
from decimal import Decimal
import json
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from w_craft_back.movie.music.providers import MusicProviderError
from w_craft_back.movie.music.providers.google_lyria import GoogleLyriaProvider
from w_craft_back.movie.music.providers.openrouter_lyria import (
    OpenRouterLyriaProvider,
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
    lines: list[bytes] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.iter_content.return_value = [content] if content else []
    response.iter_lines.return_value = lines or []
    if lines is not None:
        response.iter_content.return_value = [line + b"\n" for line in lines]
    return response


def _instrumental_request(duration: int = 30) -> dict[str, object]:
    return {
        "contentMode": "instrumental",
        "durationSeconds": duration,
        "variantCount": 1,
        "positivePrompt": "cinematic strings",
        "negativePrompt": "harsh brass",
    }


def _song_request(duration: int = 90) -> dict[str, object]:
    return {
        "contentMode": "song",
        "durationSeconds": duration,
        "variantCount": 1,
        "positivePrompt": "intimate electronic ballad",
        "negativePrompt": "distorted drums",
        "lyricsLanguage": "ru",
        "vocalStyle": {"timbre": "warm", "delivery": "intimate"},
        "lyricsSections": [
            {"type": "verse", "label": "Первый", "text": "Первая строка"},
            {"type": "chorus", "label": "Припев", "text": "Свет внутри"},
        ],
    }


@override_settings(
    MUSIC_GEMINI_TIMEOUT_SECONDS=123,
    MUSIC_OPENROUTER_TIMEOUT_SECONDS=124,
    MUSIC_MAX_OUTPUT_BYTES=1024,
    OPENROUTER_HTTP_REFERER="https://craft.example",
    OPENROUTER_APP_TITLE="Craft",
)
class LyriaProviderTests(SimpleTestCase):
    def google(
        self,
        model_name: str = "lyria-3-pro-preview",
        session: MagicMock | None = None,
    ) -> GoogleLyriaProvider:
        return GoogleLyriaProvider(
            model_name=model_name,
            session=session or MagicMock(),
            api_key="google-test-key",
            base_url="https://google.test",
        )

    def openrouter(
        self,
        model_name: str = "lyria-3-pro-preview",
        session: MagicMock | None = None,
    ) -> OpenRouterLyriaProvider:
        return OpenRouterLyriaProvider(
            model_name=model_name,
            session=session or MagicMock(),
            api_key="openrouter-test-key",
            base_url="https://openrouter.test/api/v1",
        )

    def test_capabilities_and_prices_match_each_route(self):
        direct_pro = self.google()
        routed_pro = self.openrouter()
        direct_clip = self.google("lyria-3-clip-preview")
        routed_clip = self.openrouter("lyria-3-clip-preview")

        self.assertEqual(
            direct_pro.capabilities().content_modes,
            ("instrumental", "song"),
        )
        self.assertEqual(direct_pro.capabilities().variant_counts, (1,))
        self.assertEqual(direct_pro.capabilities().min_duration_seconds, 3)
        self.assertEqual(direct_pro.capabilities().max_duration_seconds, 180)
        self.assertFalse(direct_pro.capabilities().supports_audio_reference)
        self.assertFalse(direct_pro.capabilities().supports_seed)
        self.assertEqual(direct_clip.capabilities().min_duration_seconds, 30)
        self.assertEqual(direct_clip.capabilities().max_duration_seconds, 30)
        self.assertEqual(direct_pro.pricing(1).estimated_cost, Decimal("0.08"))
        self.assertEqual(direct_clip.pricing(1).estimated_cost, Decimal("0.04"))
        self.assertEqual(routed_pro.pricing(1).estimated_cost, Decimal("0.0844"))
        self.assertEqual(routed_clip.pricing(1).estimated_cost, Decimal("0.0422"))
        self.assertEqual(routed_pro.pricing(1).snapshot["creditFeeRate"], "0.055")

    def test_invalid_model_and_variant_count_are_rejected(self):
        with self.assertRaises(MusicProviderError) as invalid:
            GoogleLyriaProvider(model_name="lyria-unknown", api_key="key")
        self.assertEqual(invalid.exception.code, "MUSIC_PROVIDER_NOT_CONFIGURED")

        with self.assertRaises(MusicProviderError) as variants:
            self.openrouter().pricing(2)
        self.assertEqual(variants.exception.code, "MUSIC_CAPABILITY_UNSUPPORTED")

    def test_configured_nonofficial_origins_are_rejected(self):
        with override_settings(MUSIC_GEMINI_API_BASE_URL="https://evil.example"):
            with self.assertRaises(MusicProviderError):
                GoogleLyriaProvider(
                    model_name="lyria-3-pro-preview",
                    api_key="key",
                )
        with override_settings(
            MUSIC_OPENROUTER_API_BASE_URL="http://openrouter.ai/api/v1"
        ):
            with self.assertRaises(MusicProviderError):
                OpenRouterLyriaProvider(
                    model_name="lyria-3-pro-preview",
                    api_key="key",
                )

    def test_google_success_parses_audio_and_compiles_instrumental_prompt(self):
        audio = b"ID3direct"
        payload = {
            "id": "google-request",
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {"type": "text", "text": "  completed   safely "},
                        {"type": "audio", "data": base64.b64encode(audio).decode()},
                    ],
                }
            ],
        }
        session = MagicMock()
        response = _response(200, content=json.dumps(payload).encode("utf-8"))
        session.post.return_value = response
        context = RecordingContext()

        submission = self.google(session=session).submit(
            _instrumental_request(),
            context,
        )

        output = submission.outputs[0]
        self.assertEqual(output.payload, audio)
        self.assertEqual(output.mime_type, "audio/mpeg")
        self.assertIsNone(output.duration_seconds)
        self.assertEqual(output.provider_request_id, "google-request")
        self.assertEqual(
            output.result_snapshot["transcriptSummary"],
            "completed safely",
        )
        call = session.post.call_args
        self.assertEqual(
            call.args[0],
            "https://google.test/v1beta/interactions",
        )
        self.assertEqual(call.kwargs["headers"]["x-goog-api-key"], "google-test-key")
        self.assertEqual(call.kwargs["json"]["model"], "lyria-3-pro-preview")
        prompt = call.kwargs["json"]["input"]
        self.assertIn("Create a 30-second piece", prompt)
        self.assertIn("Avoid: harsh brass", prompt)
        self.assertIn("Instrumental only", prompt)
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(call.kwargs["stream"])
        self.assertEqual(call.kwargs["timeout"], 123)
        self.assertGreaterEqual(context.checkpoints, 3)
        response.close.assert_called()

    def test_google_song_prompt_preserves_structured_user_lyrics(self):
        audio = base64.b64encode(b"ID3song").decode()
        response = _response(
            200,
            content=json.dumps(
                {
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [{"type": "audio", "data": audio}],
                        }
                    ]
                }
            ).encode(),
        )
        session = MagicMock()
        session.post.return_value = response

        self.google(session=session).submit(_song_request(), RecordingContext())

        prompt = session.post.call_args.kwargs["json"]["input"]
        self.assertIn("Lyrics language: Russian", prompt)
        self.assertIn("timbre: warm", prompt)
        self.assertIn("[VERSE: Первый]\nПервая строка", prompt)
        self.assertIn("[CHORUS: Припев]\nСвет внутри", prompt)

    def test_clip_rejects_non_thirty_second_request_before_post(self):
        session = MagicMock()
        with self.assertRaises(MusicProviderError) as raised:
            self.google("lyria-3-clip-preview", session).submit(
                _instrumental_request(29),
                RecordingContext(),
            )
        self.assertEqual(raised.exception.code, "MUSIC_CAPABILITY_UNSUPPORTED")
        session.post.assert_not_called()

    def test_openrouter_success_parses_streaming_audio_and_headers(self):
        first = base64.b64encode(b"ID3").decode()
        second = base64.b64encode(b"audio").decode()
        events = [
            {
                "id": "or-request",
                "choices": [
                    {
                        "delta": {
                            "audio": {"data": first, "transcript": "ready"}
                        }
                    }
                ],
            },
            {"choices": [{"delta": {"audio": {"data": second}}}]},
        ]
        lines: list[bytes] = []
        for event in events:
            lines.extend([f"data: {json.dumps(event)}".encode(), b""])
        lines.extend([b"data: [DONE]", b""])
        response = _response(200, lines=lines)
        session = MagicMock()
        session.post.return_value = response
        context = RecordingContext()

        submission = self.openrouter(session=session).submit(
            _instrumental_request(),
            context,
        )

        output = submission.outputs[0]
        self.assertEqual(output.payload, b"ID3audio")
        self.assertIsNone(output.duration_seconds)
        self.assertEqual(output.provider_request_id, "or-request")
        self.assertEqual(output.result_snapshot["transcriptSummary"], "ready")
        call = session.post.call_args
        self.assertEqual(
            call.args[0],
            "https://openrouter.test/api/v1/chat/completions",
        )
        self.assertEqual(
            call.kwargs["headers"]["Authorization"],
            "Bearer openrouter-test-key",
        )
        self.assertEqual(
            call.kwargs["headers"]["HTTP-Referer"],
            "https://craft.example",
        )
        self.assertEqual(call.kwargs["headers"]["X-Title"], "Craft")
        self.assertEqual(call.kwargs["json"]["model"], "google/lyria-3-pro-preview")
        self.assertEqual(call.kwargs["json"]["modalities"], ["text", "audio"])
        self.assertTrue(call.kwargs["json"]["stream"])
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["timeout"], 124)
        self.assertGreaterEqual(context.checkpoints, len(lines) + 2)
        response.close.assert_called()

    def test_openrouter_tolerates_final_message_audio_shape(self):
        audio = base64.b64encode(b"ID3final").decode()
        event = {
            "choices": [
                {
                    "message": {
                        "audio": {"data": audio, "transcript": "finished"}
                    }
                }
            ]
        }
        response = _response(
            200,
            lines=[
                f"data: {json.dumps(event)}".encode(),
                b"",
                b"data: [DONE]",
                b"",
            ],
        )
        session = MagicMock()
        session.post.return_value = response

        result = self.openrouter(session=session).submit(
            _instrumental_request(),
            RecordingContext(),
        )

        self.assertEqual(result.outputs[0].payload, b"ID3final")

    def test_invalid_base64_is_unknown_and_marks_confirmed_cost(self):
        google_response = _response(
            200,
            content=json.dumps(
                {
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [{"type": "audio", "data": "not-base64"}],
                        }
                    ]
                }
            ).encode(),
        )
        google_session = MagicMock()
        google_session.post.return_value = google_response
        with self.assertRaises(MusicProviderError) as google_error:
            self.google(session=google_session).submit(
                _instrumental_request(),
                RecordingContext(),
            )
        self.assertTrue(google_error.exception.outcome_unknown)
        self.assertTrue(google_error.exception.cost_incurred)

        event = {"choices": [{"delta": {"audio": {"data": "not-base64"}}}]}
        openrouter_response = _response(
            200,
            lines=[
                f"data: {json.dumps(event)}".encode(),
                b"",
                b"data: [DONE]",
                b"",
            ],
        )
        openrouter_session = MagicMock()
        openrouter_session.post.return_value = openrouter_response
        with self.assertRaises(MusicProviderError) as openrouter_error:
            self.openrouter(session=openrouter_session).submit(
                _instrumental_request(),
                RecordingContext(),
            )
        self.assertTrue(openrouter_error.exception.outcome_unknown)
        self.assertTrue(openrouter_error.exception.cost_incurred)

    @override_settings(MUSIC_MAX_OUTPUT_BYTES=4)
    def test_oversized_audio_is_rejected_as_confirmed_cost(self):
        encoded = base64.b64encode(b"ID3audio").decode()
        response = _response(
            200,
            content=json.dumps(
                {
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [{"type": "audio", "data": encoded}],
                        }
                    ]
                }
            ).encode(),
        )
        session = MagicMock()
        session.post.return_value = response

        with self.assertRaises(MusicProviderError) as raised:
            self.google(session=session).submit(
                _instrumental_request(),
                RecordingContext(),
            )

        self.assertEqual(raised.exception.code, "MUSIC_OUTPUT_TOO_LARGE")
        self.assertTrue(raised.exception.cost_incurred)

    def test_redirects_are_rejected_without_following_them(self):
        for provider_factory in (self.google, self.openrouter):
            with self.subTest(provider=provider_factory.__name__):
                session = MagicMock()
                session.post.return_value = _response(302)
                with self.assertRaises(MusicProviderError) as raised:
                    provider_factory(session=session).submit(
                        _instrumental_request(),
                        RecordingContext(),
                    )
                self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_REJECTED")
                self.assertFalse(raised.exception.retryable)
                self.assertFalse(raised.exception.outcome_unknown)

    def test_http_failures_use_definitive_status_when_available(self):
        expected_codes = {
            400: "MUSIC_PROVIDER_REJECTED",
            429: "MUSIC_PROVIDER_RATE_LIMITED",
            503: "MUSIC_PROVIDER_OUTCOME_UNKNOWN",
        }
        for provider_factory in (self.google, self.openrouter):
            for status_code, expected_code in expected_codes.items():
                with self.subTest(
                    provider=provider_factory.__name__,
                    status=status_code,
                ):
                    session = MagicMock()
                    session.post.return_value = _response(status_code)
                    with self.assertRaises(MusicProviderError) as raised:
                        provider_factory(session=session).submit(
                            _instrumental_request(),
                            RecordingContext(),
                        )
                    self.assertEqual(raised.exception.code, expected_code)
                    self.assertEqual(
                        raised.exception.outcome_unknown,
                        status_code >= 500,
                    )
                    self.assertEqual(
                        raised.exception.retryable,
                        status_code == 429,
                    )

    def test_openrouter_accepts_exact_route_model_id_without_double_prefix(self):
        provider = self.openrouter("google/lyria-3-pro-preview")

        self.assertEqual(provider.model_name, "google/lyria-3-pro-preview")
        self.assertEqual(
            provider.capabilities().model_name,
            "google/lyria-3-pro-preview",
        )

    def test_openrouter_clean_eof_without_done_is_unknown(self):
        audio = base64.b64encode(b"ID3truncated").decode()
        event = {"choices": [{"delta": {"audio": {"data": audio}}}]}
        response = _response(
            200,
            lines=[f"data: {json.dumps(event)}".encode(), b""],
        )
        session = MagicMock()
        session.post.return_value = response

        with self.assertRaises(MusicProviderError) as raised:
            self.openrouter(session=session).submit(
                _instrumental_request(),
                RecordingContext(),
            )

        self.assertTrue(raised.exception.outcome_unknown)
        self.assertTrue(raised.exception.cost_incurred)
        self.assertFalse(raised.exception.retryable)

    def test_streams_enforce_total_deadline(self):
        google_response = _response(200, content=b"{}")
        google_session = MagicMock()
        google_session.post.return_value = google_response
        with patch(
            "w_craft_back.movie.music.providers.google_lyria.monotonic",
            side_effect=[0.0, 301.0],
        ):
            with self.assertRaises(MusicProviderError) as google_error:
                self.google(session=google_session).submit(
                    _instrumental_request(),
                    RecordingContext(),
                )
        self.assertTrue(google_error.exception.outcome_unknown)
        self.assertTrue(google_error.exception.cost_incurred)

        router_response = _response(200)
        router_response.iter_content.return_value = [b"data: {}\n\n"]
        router_session = MagicMock()
        router_session.post.return_value = router_response
        with patch(
            "w_craft_back.movie.music.providers.openrouter_lyria.monotonic",
            side_effect=[0.0, 301.0],
        ):
            with self.assertRaises(MusicProviderError) as router_error:
                self.openrouter(session=router_session).submit(
                    _instrumental_request(),
                    RecordingContext(),
                )
        self.assertTrue(router_error.exception.outcome_unknown)
        self.assertTrue(router_error.exception.cost_incurred)

    def test_openrouter_stops_reading_immediately_after_done(self):
        audio = base64.b64encode(b"ID3done").decode()
        event = json.dumps(
            {"choices": [{"delta": {"audio": {"data": audio}}}]}
        )

        def chunks():
            yield f"data: {event}\n\ndata: [DONE]\n\n".encode()
            raise AssertionError("stream was read after the DONE event")

        response = _response(200)
        response.iter_content.return_value = chunks()
        session = MagicMock()
        session.post.return_value = response

        result = self.openrouter(session=session).submit(
            _instrumental_request(),
            RecordingContext(),
        )

        self.assertEqual(result.outputs[0].payload, b"ID3done")

    def test_network_failure_after_post_has_unknown_outcome(self):
        for provider_factory in (self.google, self.openrouter):
            with self.subTest(provider=provider_factory.__name__):
                session = MagicMock()
                session.post.side_effect = requests.Timeout("late timeout")
                with self.assertRaises(MusicProviderError) as raised:
                    provider_factory(session=session).submit(
                        _instrumental_request(),
                        RecordingContext(),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "MUSIC_PROVIDER_OUTCOME_UNKNOWN",
                )
                self.assertTrue(raised.exception.outcome_unknown)
                self.assertFalse(raised.exception.retryable)

    def test_success_response_disconnect_is_unknown_and_captures_cost(self):
        for provider_factory in (self.google, self.openrouter):
            with self.subTest(provider=provider_factory.__name__):
                response = _response(200)
                response.iter_content.side_effect = (
                    requests.exceptions.ChunkedEncodingError(
                        "stream disconnected"
                    )
                )
                session = MagicMock()
                session.post.return_value = response

                with self.assertRaises(MusicProviderError) as raised:
                    provider_factory(session=session).submit(
                        _instrumental_request(),
                        RecordingContext(),
                    )

                self.assertTrue(raised.exception.outcome_unknown)
                self.assertTrue(raised.exception.cost_incurred)
                self.assertFalse(raised.exception.retryable)
                response.close.assert_called()
