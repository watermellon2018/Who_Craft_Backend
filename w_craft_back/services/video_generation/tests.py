"""Provider contracts and paid-request ambiguity with no external requests."""

from dataclasses import replace
import json
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
import requests

from .adapters import create_adapter
from .contracts import (
    VideoGenerationRequest, VideoInputImage, VideoOperation,
    VideoPollResult, VideoProviderError,
)
from .registry import (
    STATIC_MODELS, compatible_model, list_models, model_from_snapshot,
    model_snapshot, nearest_duration, parse_openrouter_models,
)


GEMINI = STATIC_MODELS[0]
OPENROUTER = STATIC_MODELS[-1]
MP4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42'


def response(payload: object = None, *, status: int = 200, body: bytes = b'',
             headers: dict | None = None) -> Mock:
    value = Mock()
    value.status_code = status
    value.headers = headers or {}
    value.iter_content.return_value = [
        json.dumps(payload).encode() if payload is not None else body,
    ]
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    return value


def catalog_row(**changes: object) -> dict:
    return {
        'id': 'vendor/video-model', 'name': 'Example video',
        'supported_durations': [4, 6, 8],
        'supported_aspect_ratios': ['16:9', '9:16'],
        'supported_resolutions': ['720p', '1080p'],
        'supported_frame_images': ['first_frame', 'last_frame'],
        'generate_audio': True, **changes,
    }


class VideoRegistryTests(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()

    def tearDown(self) -> None:
        cache.clear()

    def test_nearest_duration_ties_prefer_longer(self) -> None:
        self.assertEqual(nearest_duration(GEMINI, 5), 6)
        self.assertEqual(nearest_duration(GEMINI, 4.2), 4)
        self.assertEqual(nearest_duration(GEMINI, 50), 8)

    def test_resolution_changes_legal_duration_before_launch(self) -> None:
        self.assertEqual(nearest_duration(GEMINI, 4, resolution='1080p'), 8)
        self.assertEqual(nearest_duration(GEMINI, 4, has_references=True), 8)

    def test_nonfinite_and_invalid_duration_rejected(self) -> None:
        for value in (True, 0, -1, float('inf'), float('nan'), '6'):
            with self.subTest(value=value), self.assertRaises(VideoProviderError):
                nearest_duration(GEMINI, value)

    def test_frames_and_references_are_distinct_modes(self) -> None:
        self.assertFalse(compatible_model(
            GEMINI, aspect_ratio='16:9', resolution='720p',
            has_start_frame=True, has_end_frame=True, has_references=True,
        ))
        self.assertFalse(compatible_model(
            GEMINI, aspect_ratio='16:9', resolution='720p',
            has_start_frame=False, has_end_frame=True,
        ))

    def test_catalog_unknown_capabilities_not_assumed(self) -> None:
        row = catalog_row(supported_frame_images=None, generate_audio='true')
        model = parse_openrouter_models({'data': [row]})[0]
        self.assertFalse(model.supports_start_frame)
        self.assertFalse(model.supports_end_frame)
        self.assertFalse(model.supports_audio)
        self.assertFalse(model.supports_references)

    def test_catalog_drops_untrusted_and_unusable_entries(self) -> None:
        models = parse_openrouter_models({'data': [
            catalog_row(id='../../private'),
            catalog_row(supported_durations=[True, '5', -1, 121]),
            catalog_row(supported_aspect_ratios=['javascript:bad']),
            catalog_row(supported_durations=[4, 4, 8]),
        ]})
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].durations, (4, 8))

    def test_catalog_empty_does_not_replace_prior_cache(self) -> None:
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response({'data': [catalog_row()]})):
            models = list_models(include_dynamic=True, api_key='personal-secret')
        self.assertTrue(any(
            model.provider_model == 'vendor/video-model' for model in models
        ))
        self.assertFalse(any(model.key == OPENROUTER.key for model in models))
        cache.delete('craft.video-models.openrouter.v1.refreshed')
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response({'data': []})):
            later = list_models(include_dynamic=True, api_key='personal-secret')
        self.assertEqual(models, later)

    def test_pinned_capabilities_survive_cache_loss(self) -> None:
        model = parse_openrouter_models({'data': [catalog_row()]})[0]
        snapshot = json.loads(json.dumps(model_snapshot(model)))
        cache.clear()
        self.assertEqual(model_from_snapshot(snapshot), model)


class VideoAdapterTests(SimpleTestCase):
    def setUp(self) -> None:
        self.gemini = create_adapter(
            'gemini', source='personal', api_key='gemini-secret',
        )
        self.openrouter = create_adapter(
            'openrouter', source='personal', api_key='router-secret',
        )

    def test_gemini_submit_rest_frames_and_parameters(self) -> None:
        image = VideoInputImage(b'image-bytes', 'image/png')
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response({
                       'name': 'models/veo/operations/abc',
                   })) as call:
            operation = self.gemini.submit(VideoGenerationRequest(
                GEMINI, 'Camera pans', 6, start_frame=image, end_frame=image,
            ))
        self.assertEqual(operation.id, 'models/veo/operations/abc')
        self.assertEqual(call.call_args.args[0], 'POST')
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs['headers'], {'x-goog-api-key': 'gemini-secret'})
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['json']['parameters']['durationSeconds'], 6)
        instance = kwargs['json']['instances'][0]
        self.assertEqual(instance['image'], instance['lastFrame'])
        self.assertEqual(instance['image'], {
            'mimeType': 'image/png', 'bytesBase64Encoded': 'aW1hZ2UtYnl0ZXM=',
        })
        self.assertNotIn('referenceImages', instance)
        self.assertNotIn('gemini-secret', repr(operation))

    def test_openrouter_uses_inline_frames_not_reference_mode(self) -> None:
        image = VideoInputImage(b'image-bytes', 'image/png')
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response({'id': 'gen-vid-123-abcdef'})) as call:
            self.openrouter.submit(VideoGenerationRequest(
                OPENROUTER, 'Camera pans', 8, start_frame=image, end_frame=image,
            ))
        payload = call.call_args.kwargs['json']
        self.assertEqual([row['frame_type'] for row in payload['frame_images']],
                         ['first_frame', 'last_frame'])
        self.assertTrue(payload['frame_images'][0]['image_url']['url'].startswith(
            'data:image/png;base64,',
        ))
        self.assertNotIn('input_references', payload)

    def test_submit_timeout_and_5xx_are_unknown_never_retryable(self) -> None:
        for reply in (requests.Timeout('contains secret'), response(status=500)):
            kwargs = {'side_effect': reply} if isinstance(reply, Exception) else {
                'return_value': reply,
            }
            with self.subTest(reply=type(reply).__name__), patch(
                'w_craft_back.services.video_generation.adapters.requests.request',
                **kwargs,
            ) as call, self.assertRaises(VideoProviderError) as error:
                self.gemini.submit(VideoGenerationRequest(GEMINI, 'Camera pans', 6))
            self.assertTrue(error.exception.outcome_unknown)
            self.assertFalse(error.exception.retryable)
            self.assertFalse(error.exception.safe_to_fallback)
            self.assertNotIn('secret', str(error.exception))
            call.assert_called_once()

    def test_accepted_response_without_valid_id_is_unknown(self) -> None:
        for payload in ({}, {'name': '../../evil'}, {'name': 'https://evil/abc'}):
            with patch(
                'w_craft_back.services.video_generation.adapters.requests.request',
                return_value=response(payload),
            ), self.assertRaises(VideoProviderError) as error:
                self.gemini.submit(VideoGenerationRequest(GEMINI, 'Camera pans', 6))
            self.assertTrue(error.exception.outcome_unknown)

    def test_definitive_auth_rejection_can_fallback_but_does_not_resubmit(self) -> None:
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response(status=401)) as call, self.assertRaises(
                       VideoProviderError) as error:
            self.openrouter.submit(VideoGenerationRequest(OPENROUTER, 'Camera pans', 8))
        self.assertTrue(error.exception.safe_to_fallback)
        self.assertFalse(error.exception.outcome_unknown)
        call.assert_called_once()

    def test_incompatible_request_never_reaches_network(self) -> None:
        with patch(
            'w_craft_back.services.video_generation.adapters.requests.request',
        ) as call:
            with self.assertRaises(VideoProviderError):
                self.gemini.submit(VideoGenerationRequest(
                    GEMINI, 'Camera pans', 4, resolution='1080p',
                ))
            with self.assertRaises(VideoProviderError):
                self.gemini.submit(VideoGenerationRequest(
                    GEMINI, 'Camera pans', 8, generate_audio=False,
                ))
        call.assert_not_called()

    def test_poll_is_recoverable_and_ignores_remote_urls(self) -> None:
        operation = VideoOperation('job-abc', 'openrouter', OPENROUTER.key)
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response({
                       'status': 'completed', 'unsigned_urls': ['https://evil/private'],
                   })):
            result = self.openrouter.poll(operation)
        self.assertEqual(result.download_url,
                         'https://openrouter.ai/api/v1/videos/job-abc/content?index=0')
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   side_effect=requests.Timeout), self.assertRaises(
                       VideoProviderError) as error:
            self.openrouter.poll(operation)
        self.assertTrue(error.exception.retryable)
        self.assertFalse(error.exception.outcome_unknown)

    def test_gemini_poll_extracts_rest_response(self) -> None:
        operation = VideoOperation('operations/abc', 'gemini', GEMINI.key)
        with patch('w_craft_back.services.video_generation.adapters.requests.request',
                   return_value=response({'done': True, 'response': {
                       'generateVideoResponse': {'generatedSamples': [{
                           'video': {'uri': (
                               'https://generativelanguage.googleapis.com'
                               '/v1beta/files/abc:download'
                           )},
                       }]},
                   }})):
            result = self.gemini.poll(operation)
        self.assertEqual(result.status, 'succeeded')

    @override_settings(GEMINI_API_KEY='platform-secret')
    def test_personal_missing_key_never_uses_platform_key(self) -> None:
        with self.assertRaises(VideoProviderError):
            create_adapter('gemini', source='personal')
        with self.assertRaises(VideoProviderError):
            create_adapter('gemini', source='platform', api_key='personal-secret')
        self.assertNotIn('platform-secret', repr(create_adapter('gemini')))


@override_settings(VIDEO_DOWNLOAD_ALLOWED_HOSTS=())
class VideoDownloadTests(SimpleTestCase):
    def setUp(self) -> None:
        self.adapter = create_adapter(
            'gemini', source='personal', api_key='private-key',
        )
        self.result = VideoPollResult(
            'succeeded', VideoOperation('operations/abc', 'gemini', GEMINI.key),
            download_url=(
                'https://generativelanguage.googleapis.com/v1beta/files/abc:download'
            ),
        )

    def test_redirect_to_google_storage_strips_credential(self) -> None:
        with patch(
            'w_craft_back.services.video_generation.adapters.requests.get',
            side_effect=[
                response(status=302, headers={
                    'Location': (
                        'https://storage.googleapis.com/video/movie.mp4?sig=test'
                    ),
                }),
                response(body=MP4, headers={'Content-Type': 'video/mp4'}),
            ],
        ) as call:
            result = self.adapter.download(self.result)
        self.assertEqual(result.data, MP4)
        self.assertEqual(call.call_args_list[0].kwargs['headers'],
                         {'x-goog-api-key': 'private-key'})
        self.assertEqual(call.call_args_list[1].kwargs['headers'], {})
        self.assertNotIn('sig=', repr(result))

    def test_untrusted_urls_never_receive_a_request(self) -> None:
        for url in ('http://generativelanguage.googleapis.com/file',
                    'https://evil.test/file', 'https://127.0.0.1/file',
                    'https://generativelanguage.googleapis.com:80/file',
                    'https://user@generativelanguage.googleapis.com/file'):
            with self.subTest(url=url), patch(
                'w_craft_back.services.video_generation.adapters.requests.get',
            ) as call, self.assertRaises(VideoProviderError):
                self.adapter.download(replace(self.result, download_url=url))
            call.assert_not_called()

    def test_redirect_to_private_network_is_rejected(self) -> None:
        with patch('w_craft_back.services.video_generation.adapters.requests.get',
                   return_value=response(status=302, headers={
                       'Location': 'https://127.0.0.1/secret',
                   })) as call, self.assertRaises(VideoProviderError):
            self.adapter.download(self.result)
        call.assert_called_once()

    @override_settings(VIDEO_MAX_DOWNLOAD_BYTES=10)
    def test_size_limit_applies_with_and_without_content_length(self) -> None:
        for headers in ({}, {'Content-Length': '100'}):
            with self.subTest(headers=headers), patch(
                'w_craft_back.services.video_generation.adapters.requests.get',
                return_value=response(body=MP4, headers=headers),
            ), self.assertRaises(VideoProviderError) as error:
                self.adapter.download(self.result)
            self.assertEqual(error.exception.code, 'VIDEO_DOWNLOAD_TOO_LARGE')

    def test_html_is_not_stored_as_video(self) -> None:
        with patch('w_craft_back.services.video_generation.adapters.requests.get',
                   return_value=response(body=b'<html>error</html>', headers={
                       'Content-Type': 'video/mp4',
                   })), self.assertRaises(VideoProviderError) as error:
            self.adapter.download(self.result)
        self.assertEqual(error.exception.code, 'VIDEO_DOWNLOAD_CONTENT_INVALID')
