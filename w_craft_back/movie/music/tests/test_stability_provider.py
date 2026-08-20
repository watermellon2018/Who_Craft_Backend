from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from unittest.mock import MagicMock

import requests
from django.test import SimpleTestCase, override_settings

from w_craft_back.movie.music.providers import MusicProviderError
from w_craft_back.movie.music.providers.registry import get_music_provider
from w_craft_back.movie.music.providers.stability import StabilityAudioProvider


GENERATION_ID = "a" * 64


class RecordingContext:
    def __init__(self) -> None:
        self.checkpoints = 0

    def heartbeat(self) -> None:
        self.checkpoints += 1

    def is_cancelled(self) -> bool:
        return False

    def checkpoint(self) -> None:
        self.checkpoints += 1


def _response(status_code: int, *, content: bytes = b"", payload=None):
    if payload is not None and not content:
        content = json.dumps(payload).encode("utf-8")
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.json.return_value = payload
    response.headers = {}
    response.iter_content.return_value = [content] if content else []
    return response


@override_settings(
    MUSIC_STABILITY_MODEL="stable-audio-3",
    MUSIC_STABILITY_OUTPUT_FORMAT="mp3",
    MUSIC_STABILITY_TIMEOUT_SECONDS=12,
    MUSIC_STABILITY_POLL_SECONDS=10,
    MUSIC_STABILITY_MAX_POLL_SECONDS=1800,
    MUSIC_STABILITY_COST_USD_PER_VARIANT="0.26",
)
class StabilityAudioProviderTests(SimpleTestCase):
    def provider(self, session: MagicMock | None = None) -> StabilityAudioProvider:
        return StabilityAudioProvider(
            session=session or MagicMock(),
            api_key="test-key",
            base_url="https://api.stability.test",
        )

    def test_capabilities_only_advertise_supported_product_contract(self):
        capabilities = self.provider().capabilities()

        self.assertEqual(capabilities.content_modes, ("instrumental",))
        self.assertEqual(capabilities.variant_counts, (1,))
        self.assertEqual(capabilities.output_formats, ("mp3",))
        self.assertFalse(capabilities.supports_audio_reference)
        self.assertFalse(capabilities.supports_cancellation)
        self.assertTrue(capabilities.supports_external_async)

    def test_pricing_uses_provider_native_fixed_cost(self):
        pricing = self.provider().pricing(1)

        self.assertEqual(pricing.estimated_cost, Decimal("0.26"))
        self.assertEqual(pricing.snapshot["providerCreditsPerVariant"], 26)
        self.assertEqual(pricing.snapshot["markup"], "0")

    @override_settings(MUSIC_STABILITY_COST_USD_PER_VARIANT="0")
    def test_unpriced_paid_generation_is_rejected(self):
        with self.assertRaises(MusicProviderError) as raised:
            self.provider().pricing(1)

        self.assertEqual(raised.exception.code, "GENERATION_PRICE_UNAVAILABLE")

    def test_submit_starts_async_generation_without_exposing_key(self):
        session = MagicMock()
        session.post.return_value = _response(202, payload={"id": GENERATION_ID})
        context = RecordingContext()

        submission = self.provider(session).submit(
            {
                "positivePrompt": "cinematic strings",
                "negativePrompt": "vocals",
                "durationSeconds": 30,
                "baseSeed": 4_294_967_295,
            },
            context,
        )

        self.assertEqual(submission.external_job_id, GENERATION_ID)
        self.assertEqual(submission.provider_metadata["seed"], 4_294_967_294)
        self.assertEqual(submission.provider_metadata["pollCount"], 0)
        self.assertIn("pollStartedAt", submission.provider_metadata)
        self.assertEqual(submission.poll_after_seconds, 10)
        request = session.post.call_args
        self.assertEqual(
            request.args[0],
            "https://api.stability.test/v2beta/audio/stable-audio/text-to-audio",
        )
        multipart = request.kwargs["files"]
        self.assertEqual(multipart["duration"], (None, "30"))
        self.assertEqual(multipart["output_format"], (None, "mp3"))
        self.assertIn("Avoid: vocals", multipart["prompt"][1])
        self.assertFalse(request.kwargs["allow_redirects"])
        self.assertEqual(request.kwargs["timeout"], 12)
        self.assertTrue(request.kwargs["stream"])
        self.assertGreaterEqual(context.checkpoints, 2)

    def test_poll_preserves_metadata_while_pending(self):
        session = MagicMock()
        session.get.return_value = _response(202)
        metadata = {"seed": 123, "outputFormat": "mp3"}

        submission = self.provider(session).poll(
            GENERATION_ID,
            RecordingContext(),
            metadata,
        )

        self.assertEqual(submission.external_job_id, GENERATION_ID)
        self.assertEqual(submission.provider_metadata["seed"], 123)
        self.assertEqual(submission.provider_metadata["outputFormat"], "mp3")
        self.assertEqual(submission.provider_metadata["pollCount"], 1)
        self.assertIn("pollStartedAt", submission.provider_metadata)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_poll_timeout_keeps_the_known_generation_handle(self):
        session = MagicMock()
        session.get.side_effect = requests.Timeout("temporary poll failure")

        submission = self.provider(session).poll(
            GENERATION_ID,
            RecordingContext(),
            {"seed": 123},
        )

        self.assertEqual(submission.external_job_id, GENERATION_ID)
        self.assertEqual(submission.provider_metadata["seed"], 123)
        self.assertEqual(submission.provider_metadata["pollCount"], 1)

    def test_poll_deadline_terminates_unknown_generation_without_request(self):
        session = MagicMock()
        started_at = datetime.now(timezone.utc) - timedelta(seconds=1801)

        with self.assertRaises(MusicProviderError) as raised:
            self.provider(session).poll(
                GENERATION_ID,
                RecordingContext(),
                {"seed": 123, "pollStartedAt": started_at.isoformat()},
            )

        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_OUTCOME_UNKNOWN")
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertFalse(raised.exception.retryable)
        session.get.assert_not_called()

    def test_poll_returns_audio_bytes_and_provenance(self):
        session = MagicMock()
        session.get.return_value = _response(200, content=b"ID3audio")

        submission = self.provider(session).poll(
            GENERATION_ID,
            RecordingContext(),
            {"seed": 123},
        )

        output = submission.outputs[0]
        self.assertEqual(output.payload, b"ID3audio")
        self.assertEqual(output.mime_type, "audio/mpeg")
        self.assertIsNone(output.duration_seconds)
        self.assertEqual(output.seed, 123)
        self.assertEqual(output.provider_request_id, GENERATION_ID)
        self.assertEqual(output.provenance["provider"], "stability")

    @override_settings(MUSIC_STABILITY_OUTPUT_FORMAT="wav")
    def test_poll_uses_durable_job_format_after_configuration_change(self):
        session = MagicMock()
        session.get.return_value = _response(200, content=b"ID3audio")

        submission = self.provider(session).poll(
            GENERATION_ID,
            RecordingContext(),
            {"outputFormat": "mp3", "seed": 123},
        )

        output = submission.outputs[0]
        self.assertEqual(output.mime_type, "audio/mpeg")
        self.assertEqual(output.result_snapshot["outputFormat"], "mp3")

    @override_settings(MUSIC_MAX_OUTPUT_BYTES=4)
    def test_poll_rejects_oversized_audio_before_buffering_it(self):
        session = MagicMock()
        session.get.return_value = _response(200, content=b"ID3audio")

        with self.assertRaises(MusicProviderError) as raised:
            self.provider(session).poll(
                GENERATION_ID,
                RecordingContext(),
                {"seed": 123},
            )

        self.assertEqual(raised.exception.code, "MUSIC_OUTPUT_TOO_LARGE")
        self.assertTrue(raised.exception.cost_incurred)

    def test_submit_timeout_is_outcome_unknown_and_not_retryable(self):
        session = MagicMock()
        session.post.side_effect = requests.Timeout("late response")

        with self.assertRaises(MusicProviderError) as raised:
            self.provider(session).submit(
                {
                    "positivePrompt": "ambient",
                    "durationSeconds": 30,
                    "baseSeed": 1,
                },
                RecordingContext(),
            )

        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_OUTCOME_UNKNOWN")
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertFalse(raised.exception.retryable)

    def test_submit_server_error_is_outcome_unknown_and_not_retryable(self):
        session = MagicMock()
        session.post.return_value = _response(503)

        with self.assertRaises(MusicProviderError) as raised:
            self.provider(session).submit(
                {
                    "positivePrompt": "ambient",
                    "durationSeconds": 30,
                    "baseSeed": 1,
                },
                RecordingContext(),
            )

        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_OUTCOME_UNKNOWN")
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertFalse(raised.exception.retryable)

    def test_submit_rejects_oversized_json_response(self):
        session = MagicMock()
        session.post.return_value = _response(
            202,
            content=b"x" * (64 * 1024 + 1),
        )

        with self.assertRaises(MusicProviderError) as raised:
            self.provider(session).submit(
                {
                    "positivePrompt": "ambient",
                    "durationSeconds": 30,
                    "baseSeed": 1,
                },
                RecordingContext(),
            )

        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_OUTCOME_UNKNOWN")

    def test_submit_body_disconnect_is_outcome_unknown_and_not_retryable(self):
        session = MagicMock()
        response = _response(202)
        response.iter_content.side_effect = requests.exceptions.ChunkedEncodingError(
            "response body disconnected"
        )
        session.post.return_value = response

        with self.assertRaises(MusicProviderError) as raised:
            self.provider(session).submit(
                {
                    "positivePrompt": "ambient",
                    "durationSeconds": 30,
                    "baseSeed": 1,
                },
                RecordingContext(),
            )

        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_OUTCOME_UNKNOWN")
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertFalse(raised.exception.retryable)

    def test_rate_limit_is_retryable(self):
        session = MagicMock()
        session.post.return_value = _response(429, payload={"errors": ["slow"]})

        with self.assertRaises(MusicProviderError) as raised:
            self.provider(session).submit(
                {
                    "positivePrompt": "ambient",
                    "durationSeconds": 30,
                    "baseSeed": 1,
                },
                RecordingContext(),
            )

        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_RATE_LIMITED")
        self.assertTrue(raised.exception.retryable)

    @override_settings(
        MUSIC_GENERATION_PROVIDER="stability",
        STABILITY_API_KEY="",
    )
    def test_registry_requires_stability_credentials(self):
        with self.assertRaises(MusicProviderError) as raised:
            get_music_provider()

        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_NOT_CONFIGURED")

    @override_settings(MUSIC_STABILITY_API_BASE_URL="https://evil.example")
    def test_configured_api_origin_cannot_redirect_the_bearer_key(self):
        with self.assertRaises(MusicProviderError) as raised:
            StabilityAudioProvider(api_key="test-key")

        self.assertEqual(raised.exception.code, "MUSIC_PROVIDER_NOT_CONFIGURED")
