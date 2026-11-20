"""Bounded HTTP adapters; a paid POST is never automatically retried."""

import base64
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.views.decorators.debug import sensitive_variables
import requests

from .contracts import (
    VideoDownload, VideoGenerationRequest, VideoInputImage, VideoOperation,
    VideoPollResult, VideoProviderError,
)
from .registry import compatible_model, nearest_duration


_OPENROUTER_BASE = 'https://openrouter.ai/api/v1'
_GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta'
_OPERATION_ID = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
_GEMINI_OPERATION = re.compile(
    r'(?:models/[A-Za-z0-9._-]+/)?operations/[A-Za-z0-9_-]{1,200}\Z',
)
_REDIRECTS = {301, 302, 303, 307, 308}


def _http_error(status: int, *, submitting: bool) -> VideoProviderError:
    uncertain = submitting and (status >= 500 or status in {408, 409})
    code = {
        400: 'VIDEO_PROVIDER_REJECTED', 401: 'VIDEO_AUTHENTICATION_FAILED',
        402: 'VIDEO_PROVIDER_BALANCE_REQUIRED', 403: 'VIDEO_PROVIDER_FORBIDDEN',
        404: 'VIDEO_PROVIDER_NOT_FOUND', 413: 'VIDEO_INPUT_TOO_LARGE',
        422: 'VIDEO_PROVIDER_REJECTED', 429: 'VIDEO_PROVIDER_RATE_LIMITED',
    }.get(status, 'VIDEO_PROVIDER_UNAVAILABLE')
    return VideoProviderError(
        'VIDEO_OUTCOME_UNKNOWN' if uncertain else code,
        outcome_unknown=uncertain,
        retryable=not submitting and (status >= 500 or status in {408, 429}),
        safe_to_fallback=submitting and status in {400, 401, 402, 403, 404, 422, 429},
    )


def _transport_error(*, submitting: bool) -> VideoProviderError:
    return VideoProviderError(
        'VIDEO_OUTCOME_UNKNOWN' if submitting else 'VIDEO_PROVIDER_UNAVAILABLE',
        outcome_unknown=submitting, retryable=not submitting,
    )


def _encoded(image: VideoInputImage) -> str:
    if (
        image.content_type not in {'image/png', 'image/jpeg', 'image/webp'}
        or not isinstance(image.data, bytes) or not image.data
        or len(image.data) > 20 * 1024 * 1024
    ):
        raise VideoProviderError('VIDEO_INPUT_INVALID')
    return base64.b64encode(image.data).decode('ascii')


def validate_request(request: VideoGenerationRequest) -> None:
    if (
        not isinstance(request.prompt, str) or not request.prompt.strip()
        or len(request.prompt) > 24000 or type(request.duration) is not int
        or type(request.generate_audio) is not bool
        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,180}',
                            request.model.provider_model)
        or '..' in request.model.provider_model
        or not compatible_model(
            request.model, aspect_ratio=request.aspect_ratio,
            resolution=request.resolution,
            has_start_frame=request.start_frame is not None,
            has_end_frame=request.end_frame is not None,
            has_references=bool(request.reference_images),
        )
        or len(request.reference_images) > request.model.max_references
        or (request.model.audio_always_on and not request.generate_audio)
        or (request.generate_audio and not request.model.supports_audio)
    ):
        raise VideoProviderError('VIDEO_PARAMETERS_INCOMPATIBLE')
    if nearest_duration(
        request.model, request.duration, resolution=request.resolution,
        has_end_frame=request.end_frame is not None,
        has_references=bool(request.reference_images),
    ) != request.duration:
        raise VideoProviderError('VIDEO_PARAMETERS_INCOMPATIBLE')
    images = [request.start_frame, request.end_frame, *request.reference_images]
    if sum(len(image.data) for image in images if image is not None) > 32 * 1024 * 1024:
        raise VideoProviderError('VIDEO_INPUT_TOO_LARGE')


class VideoAdapter:
    provider = ''
    base_url = ''

    @sensitive_variables('api_key')
    def __init__(self, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise VideoProviderError('VIDEO_PROVIDER_NOT_CONFIGURED')
        self._api_key = api_key.strip()

    def __repr__(self) -> str:
        return f'<VideoAdapter provider={self.provider}>'

    def _headers(self) -> dict[str, str]:
        if self.provider == 'gemini':
            return {'x-goog-api-key': self._api_key}
        return {'Authorization': f'Bearer {self._api_key}'}

    @sensitive_variables()
    def _json(self, method: str, url: str, *, payload: dict | None = None) -> dict:
        submitting = method == 'POST'
        timeout = max(1, min(float(getattr(
            settings, 'VIDEO_HTTP_TIMEOUT_SECONDS', 45,
        )), 120))
        try:
            with requests.request(
                method, url, json=payload, headers=self._headers(),
                timeout=(10, timeout), allow_redirects=False, stream=True,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise _http_error(response.status_code, submitting=submitting)
                body = self._read_body(response, 4 * 1024 * 1024, timeout)
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except requests.RequestException:
            raise _transport_error(submitting=submitting) from None
        except (ValueError, UnicodeError):
            raise VideoProviderError(
                'VIDEO_OUTCOME_UNKNOWN' if submitting
                else 'VIDEO_PROVIDER_RESPONSE_INVALID',
                outcome_unknown=submitting, retryable=not submitting,
            ) from None
        except VideoProviderError as exc:
            if submitting and exc.code in {
                'VIDEO_DOWNLOAD_TOO_LARGE', 'VIDEO_DOWNLOAD_TIMEOUT',
                'VIDEO_PROVIDER_RESPONSE_INVALID',
            }:
                raise _transport_error(submitting=True) from None
            raise

    @staticmethod
    def _read_body(response: requests.Response, limit: int, deadline: float) -> bytes:
        try:
            declared_size = int(response.headers.get('Content-Length', '0'))
        except (TypeError, ValueError):
            raise VideoProviderError('VIDEO_PROVIDER_RESPONSE_INVALID') from None
        if declared_size < 0 or declared_size > limit:
            raise VideoProviderError('VIDEO_DOWNLOAD_TOO_LARGE')
        chunks = []
        size = 0
        started = time.monotonic()
        for chunk in response.iter_content(chunk_size=65536):
            if time.monotonic() - started > deadline:
                raise VideoProviderError('VIDEO_DOWNLOAD_TIMEOUT', retryable=True)
            size += len(chunk)
            if size > limit:
                raise VideoProviderError('VIDEO_DOWNLOAD_TOO_LARGE')
            chunks.append(chunk)
        return b''.join(chunks)

    def _check_operation(self, operation: VideoOperation) -> None:
        pattern = _GEMINI_OPERATION if self.provider == 'gemini' else _OPERATION_ID
        if operation.provider != self.provider or not pattern.fullmatch(operation.id):
            raise VideoProviderError('VIDEO_OPERATION_INVALID')

    @sensitive_variables()
    def download(self, result: VideoPollResult) -> VideoDownload:
        self._check_operation(result.operation)
        if result.status != 'succeeded':
            raise VideoProviderError('VIDEO_RESULT_NOT_READY')
        url = result.download_url
        initial_host = urlsplit(self.base_url).hostname
        allowed_hosts = {initial_host, 'storage.googleapis.com'}
        allowed_hosts.update(getattr(settings, 'VIDEO_DOWNLOAD_ALLOWED_HOSTS', ()))
        limit = min(max(int(getattr(
            settings, 'VIDEO_MAX_DOWNLOAD_BYTES', 134217728,
        )), 1), 536870912)
        deadline = min(max(float(getattr(
            settings, 'VIDEO_DOWNLOAD_TIMEOUT_SECONDS', 180,
        )), 1), 600)
        started = time.monotonic()
        # Credentials are sent only to the provider API host, never a CDN.
        try:
            for _ in range(4):
                parts = urlsplit(url)
                if (
                    parts.scheme != 'https' or parts.hostname not in allowed_hosts
                    or parts.username or parts.password or parts.port not in {None, 443}
                    or parts.fragment or len(url) > 8192
                ):
                    raise VideoProviderError('VIDEO_DOWNLOAD_URL_REJECTED')
                remaining = deadline - (time.monotonic() - started)
                if remaining <= 0:
                    raise VideoProviderError('VIDEO_DOWNLOAD_TIMEOUT', retryable=True)
                headers = self._headers() if parts.hostname == initial_host else {}
                with requests.get(
                    url, headers=headers, stream=True, allow_redirects=False,
                    timeout=(min(10, remaining), min(45, remaining)),
                ) as response:
                    if response.status_code in _REDIRECTS:
                        url = urljoin(url, response.headers.get('Location', ''))
                        continue
                    if response.status_code != 200:
                        raise _http_error(response.status_code, submitting=False)
                    data = self._read_body(response, limit, remaining)
                    content_type = response.headers.get(
                        'Content-Type', '',
                    ).split(';')[0].strip().lower()
                if len(data) >= 12 and data[4:8] == b'ftyp':
                    detected = 'video/mp4'
                elif data.startswith(b'\x1a\x45\xdf\xa3'):
                    detected = 'video/webm'
                else:
                    raise VideoProviderError('VIDEO_DOWNLOAD_CONTENT_INVALID')
                if content_type not in {detected, 'application/octet-stream', ''}:
                    raise VideoProviderError('VIDEO_DOWNLOAD_CONTENT_INVALID')
                return VideoDownload(data, detected, {'sizeBytes': len(data)})
        except requests.RequestException:
            raise _transport_error(submitting=False) from None
        except ValueError:
            raise VideoProviderError('VIDEO_DOWNLOAD_URL_REJECTED') from None
        raise VideoProviderError('VIDEO_DOWNLOAD_REDIRECT_LIMIT')


class GeminiVideoAdapter(VideoAdapter):
    provider = 'gemini'
    base_url = _GEMINI_BASE

    def submit(self, request: VideoGenerationRequest) -> VideoOperation:
        validate_request(request)
        if request.model.provider != self.provider:
            raise VideoProviderError('VIDEO_MODEL_INCOMPATIBLE')
        instance = {'prompt': request.prompt}
        for name, image in (
            ('image', request.start_frame), ('lastFrame', request.end_frame),
        ):
            if image is not None:
                # Match google-genai 2.25's _Image_to_mldev REST converter;
                # the prose guide's inlineData example differs from the SDK.
                instance[name] = {
                    'mimeType': image.content_type,
                    'bytesBase64Encoded': _encoded(image),
                }
        if request.reference_images:
            instance['referenceImages'] = [{
                'image': {
                    'mimeType': image.content_type,
                    'bytesBase64Encoded': _encoded(image),
                },
                'referenceType': 'asset',
            } for image in request.reference_images]
        payload = {'instances': [instance], 'parameters': {
            'sampleCount': 1, 'durationSeconds': request.duration,
            'aspectRatio': request.aspect_ratio, 'resolution': request.resolution,
            'personGeneration': 'allow_adult'
            if request.start_frame or request.reference_images else 'allow_all',
        }}
        result = self._json(
            'POST', f'{self.base_url}/models/'
            f'{request.model.provider_model}:predictLongRunning',
            payload=payload,
        )
        operation_id = result.get('name')
        if (
            not isinstance(operation_id, str)
            or not _GEMINI_OPERATION.fullmatch(operation_id)
        ):
            raise VideoProviderError('VIDEO_OUTCOME_UNKNOWN', outcome_unknown=True)
        return VideoOperation(operation_id, self.provider, request.model.key)

    def poll(self, operation: VideoOperation) -> VideoPollResult:
        self._check_operation(operation)
        result = self._json('GET', f'{self.base_url}/{operation.id}')
        if result.get('error'):
            return VideoPollResult(
                'failed', operation, error_code='VIDEO_PROVIDER_GENERATION_FAILED',
            )
        if result.get('done') is not True:
            return VideoPollResult('processing', operation)
        response = result.get('response') or {}
        output = response.get('generateVideoResponse') if isinstance(
            response, dict,
        ) else None
        samples = output.get('generatedSamples') if isinstance(output, dict) else None
        if (
            not isinstance(samples, list) or not samples
            or not isinstance(samples[0], dict)
        ):
            return VideoPollResult(
                'failed', operation, error_code='VIDEO_PROVIDER_NO_OUTPUT',
            )
        video = samples[0].get('video')
        url = video.get('uri') if isinstance(video, dict) else None
        if not isinstance(url, str) or not url:
            return VideoPollResult(
                'failed', operation, error_code='VIDEO_PROVIDER_NO_OUTPUT',
            )
        return VideoPollResult('succeeded', operation, download_url=url)


class OpenRouterVideoAdapter(VideoAdapter):
    provider = 'openrouter'
    base_url = _OPENROUTER_BASE

    def catalog(self) -> dict:
        return self._json('GET', f'{self.base_url}/videos/models')

    def submit(self, request: VideoGenerationRequest) -> VideoOperation:
        validate_request(request)
        if request.model.provider != self.provider:
            raise VideoProviderError('VIDEO_MODEL_INCOMPATIBLE')
        payload = {
            'model': request.model.provider_model, 'prompt': request.prompt,
            'duration': request.duration, 'aspect_ratio': request.aspect_ratio,
            'resolution': request.resolution,
        }
        if request.model.supports_audio:
            payload['generate_audio'] = request.generate_audio
        frames = []
        for frame_type, image in (
            ('first_frame', request.start_frame), ('last_frame', request.end_frame),
        ):
            if image is not None:
                frames.append({
                    'type': 'image_url', 'frame_type': frame_type,
                    'image_url': {
                        'url': f'data:{image.content_type};base64,{_encoded(image)}',
                    },
                })
        if frames:
            payload['frame_images'] = frames
        result = self._json('POST', f'{self.base_url}/videos', payload=payload)
        operation_id = result.get('id')
        if (
            not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
        ):
            raise VideoProviderError('VIDEO_OUTCOME_UNKNOWN', outcome_unknown=True)
        return VideoOperation(operation_id, self.provider, request.model.key)

    def poll(self, operation: VideoOperation) -> VideoPollResult:
        self._check_operation(operation)
        result = self._json('GET', f'{self.base_url}/videos/{operation.id}')
        if result.get('status') == 'completed':
            # Ignore supplied polling/content URLs: derive the authenticated
            # content endpoint from the already validated opaque operation ID.
            return VideoPollResult(
                'succeeded', operation,
                download_url=f'{self.base_url}/videos/{operation.id}/content?index=0',
            )
        if result.get('status') in {'failed', 'cancelled', 'expired'}:
            return VideoPollResult(
                'failed', operation, error_code='VIDEO_PROVIDER_GENERATION_FAILED',
            )
        if result.get('status') not in {'pending', 'in_progress'}:
            raise VideoProviderError('VIDEO_PROVIDER_RESPONSE_INVALID', retryable=True)
        return VideoPollResult('processing', operation)


@sensitive_variables('api_key')
def create_adapter(
    provider: str, *, api_key: str | None = None, source: str = 'platform',
) -> VideoAdapter:
    adapters = {'gemini': GeminiVideoAdapter, 'openrouter': OpenRouterVideoAdapter}
    if provider not in adapters or source not in {'platform', 'personal'}:
        raise VideoProviderError('VIDEO_PROVIDER_UNSUPPORTED')
    if source == 'personal':
        if not api_key:
            raise VideoProviderError('VIDEO_PROVIDER_NOT_CONFIGURED')
    else:
        # An explicit key must never accidentally change the Craft payer.
        if api_key is not None:
            raise VideoProviderError('VIDEO_CREDENTIAL_SOURCE_INVALID')
        api_key = getattr(settings, {
            'gemini': 'GEMINI_API_KEY', 'openrouter': 'OPENROUTER_API_KEY',
        }[provider], '')
    return adapters[provider](api_key)
