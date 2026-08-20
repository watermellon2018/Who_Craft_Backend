from decimal import Decimal
from unittest.mock import Mock

import requests
from django.test import SimpleTestCase, override_settings

from w_craft_back.movie.sound_effects.errors import SoundEffectProviderError
from w_craft_back.movie.sound_effects.providers.elevenlabs import (
    ElevenLabsSoundEffectsProvider,
)

from .helpers import mp3_bytes, request_payload


class Context:
    def __init__(self):
        self.checkpoints = 0

    def checkpoint(self):
        self.checkpoints += 1


def response(status_code=200, payload=b"", headers=None):
    result = Mock(status_code=status_code, headers=headers or {})
    result.iter_content.return_value = iter((payload,))
    return result


@override_settings(
    SOUND_EFFECTS_ELEVENLABS_COST_USD_PER_MINUTE="0.12",
    SOUND_EFFECTS_ELEVENLABS_AUTO_COST_USD="0.06",
    SOUND_EFFECTS_ELEVENLABS_OUTPUT_FORMAT="mp3_44100_128",
    SOUND_EFFECTS_ELEVENLABS_TIMEOUT_SECONDS=60,
    SOUND_EFFECTS_ELEVENLABS_RESPONSE_DEADLINE_SECONDS=180,
)
class ElevenLabsSoundEffectProviderTests(SimpleTestCase):
    def provider(self, session=None):
        return ElevenLabsSoundEffectsProvider(
            session=session,
            api_key="test-key",
            base_url="https://elevenlabs.test",
        )

    def test_pricing_uses_duration_rate_and_auto_reservation(self):
        provider = self.provider()

        self.assertEqual(
            provider.pricing(30).estimated_cost,
            Decimal("0.06"),
        )
        self.assertEqual(
            provider.pricing(None).estimated_cost,
            Decimal("0.06"),
        )

    def test_posts_bounded_v2_request_and_returns_one_mp3(self):
        session = Mock()
        upstream = response(
            payload=mp3_bytes(),
            headers={"request-id": "req-1"},
        )
        session.post.return_value = upstream
        context = Context()

        generated = self.provider(session).generate(request_payload(), context)

        self.assertEqual(generated.mime_type, "audio/mpeg")
        self.assertEqual(generated.provider_request_id, "req-1")
        call = session.post.call_args
        self.assertEqual(call.args[0], "https://elevenlabs.test/v1/sound-generation")
        self.assertEqual(call.kwargs["json"]["model_id"], "eleven_text_to_sound_v2")
        self.assertEqual(call.kwargs["json"]["duration_seconds"], 2.5)
        self.assertEqual(call.kwargs["params"]["output_format"], "mp3_44100_128")
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertTrue(call.kwargs["stream"])
        self.assertGreaterEqual(context.checkpoints, 3)
        upstream.close.assert_called_once()

    def test_provider_request_id_is_bounded_for_database_storage(self):
        session = Mock()
        session.post.return_value = response(
            payload=mp3_bytes(),
            headers={"request-id": "r" * 500},
        )

        generated = self.provider(session).generate(request_payload(), Context())

        self.assertEqual(generated.provider_request_id, "r" * 255)

    def test_auto_duration_is_omitted(self):
        session = Mock()
        session.post.return_value = response(payload=mp3_bytes())

        self.provider(session).generate(
            request_payload(durationSeconds=None),
            Context(),
        )

        self.assertNotIn("duration_seconds", session.post.call_args.kwargs["json"])

    def test_ambiguous_post_timeout_is_terminal_and_billable(self):
        session = Mock()
        session.post.side_effect = requests.ReadTimeout("late response")

        with self.assertRaises(SoundEffectProviderError) as raised:
            self.provider(session).generate(request_payload(), Context())

        self.assertEqual(
            raised.exception.code,
            "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
        )
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertTrue(raised.exception.cost_incurred)
        self.assertFalse(raised.exception.retryable)

    def test_http_500_is_treated_as_ambiguous_paid_post(self):
        session = Mock()
        upstream = response(status_code=500)
        session.post.return_value = upstream

        with self.assertRaises(SoundEffectProviderError) as raised:
            self.provider(session).generate(request_payload(), Context())

        self.assertTrue(raised.exception.outcome_unknown)
        self.assertTrue(raised.exception.cost_incurred)
        upstream.close.assert_called_once()

    def test_redirect_is_rejected_without_following_location(self):
        session = Mock()
        upstream = response(status_code=302, headers={"Location": "https://evil.test"})
        session.post.return_value = upstream

        with self.assertRaises(SoundEffectProviderError) as raised:
            self.provider(session).generate(request_payload(), Context())

        self.assertEqual(raised.exception.code, "SOUND_EFFECT_PROVIDER_REJECTED")
        upstream.close.assert_called_once()

    def test_declared_or_streamed_oversize_is_rejected_boundedly(self):
        session = Mock()
        upstream = response(
            payload=b"unused",
            headers={"Content-Length": str(50 * 1024 * 1024 + 1)},
        )
        session.post.return_value = upstream

        with self.assertRaises(SoundEffectProviderError) as raised:
            self.provider(session).generate(request_payload(), Context())

        self.assertEqual(raised.exception.code, "SOUND_EFFECT_OUTPUT_TOO_LARGE")
        upstream.iter_content.assert_not_called()
        upstream.close.assert_called_once()

    def test_stream_disconnect_after_paid_post_is_outcome_unknown(self):
        session = Mock()
        upstream = response()
        upstream.iter_content.side_effect = requests.exceptions.ChunkedEncodingError(
            "disconnect"
        )
        session.post.return_value = upstream

        with self.assertRaises(SoundEffectProviderError) as raised:
            self.provider(session).generate(request_payload(), Context())

        self.assertEqual(
            raised.exception.code,
            "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN",
        )
        self.assertTrue(raised.exception.cost_incurred)
        upstream.close.assert_called_once()
