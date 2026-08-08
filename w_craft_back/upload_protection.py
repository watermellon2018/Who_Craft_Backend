"""Pre-parse request and file limits for upload-capable endpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, BinaryIO

from django.conf import settings
from django.core.files.uploadhandler import TemporaryFileUploadHandler
from django.http import HttpRequest, JsonResponse


_INSTALLED_ATTR = "_bounded_upload_protection_installed"


class UploadLimitExceeded(Exception):
    """Raised while parsing, before an endpoint can observe partial data."""


class BoundedTemporaryUploadHandler(TemporaryFileUploadHandler):
    """Stream files to disk and delete every partial file on overflow."""

    chunk_size = 64 * 1024

    def __init__(self, request: HttpRequest, max_file_bytes: int) -> None:
        super().__init__(request)
        self.max_file_bytes = max_file_bytes
        self.received_bytes = 0
        self._temporary_files: list[Any] = []

    def new_file(self, *args, **kwargs) -> None:
        super().new_file(*args, **kwargs)
        self._temporary_files.append(self.file)

    def receive_data_chunk(self, raw_data: bytes, start: int):
        self.received_bytes += len(raw_data)
        if self.received_bytes > self.max_file_bytes:
            self.abort()
            raise UploadLimitExceeded
        return super().receive_data_chunk(raw_data, start)

    def upload_interrupted(self) -> None:
        self.abort()

    def abort(self) -> None:
        for temporary_file in self._temporary_files:
            temporary_file.close()
        self._temporary_files.clear()


class BoundedRequestStream:
    """Count every body byte and abort parsing immediately at the cap."""

    def __init__(
        self,
        stream: BinaryIO,
        max_body_bytes: int,
        abort: Callable[[], None],
    ) -> None:
        self._stream = stream
        self.max_body_bytes = max_body_bytes
        self.bytes_read = 0
        self._abort = abort

    def read(self, size: int = -1) -> bytes:
        return self._limited_read(self._stream.read, size)

    def readline(self, size: int = -1) -> bytes:
        return self._limited_read(self._stream.readline, size)

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def _limited_read(
        self,
        reader: Callable[[int], bytes],
        size: int,
    ) -> bytes:
        remaining = self.max_body_bytes - self.bytes_read
        probe_size = remaining + 1
        requested_size = (
            probe_size if size is None or size < 0 else min(size, probe_size)
        )
        data = reader(requested_size)
        if len(data) > remaining:
            self._abort()
            raise UploadLimitExceeded
        self.bytes_read += len(data)
        return data

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


class UploadProtectionMiddleware:
    """Install request limits after URL resolution and before DRF parsing."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        del view_func, view_args, view_kwargs
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None

        match = request.resolver_match
        url_name = match.url_name if match is not None else None
        max_file_bytes = settings.UPLOAD_ENDPOINT_FILE_LIMITS.get(
            url_name,
            settings.UPLOAD_DEFAULT_MULTIPART_FILE_LIMIT_BYTES,
        )
        body_limit = (
            max_file_bytes + settings.UPLOAD_MULTIPART_OVERHEAD_BYTES
        )
        content_type = request.content_type or ""
        is_multipart = content_type.lower().startswith("multipart/form-data")
        declared_size = self._declared_size(request)
        if is_multipart and declared_size is None:
            return self._too_large_response()
        if declared_size is not None and declared_size > body_limit:
            return self._too_large_response()

        if getattr(request, _INSTALLED_ATTR, False):
            return None

        upload_handler = None
        if is_multipart:
            upload_handler = BoundedTemporaryUploadHandler(
                request,
                max_file_bytes,
            )
            request.upload_handlers = [upload_handler]

        abort = upload_handler.abort if upload_handler else lambda: None
        request._stream = BoundedRequestStream(
            request._stream,
            body_limit,
            abort,
        )
        setattr(request, _INSTALLED_ATTR, True)
        return None

    def process_exception(self, request, exception):
        del request
        if isinstance(exception, UploadLimitExceeded):
            return self._too_large_response()
        return None

    @staticmethod
    def _declared_size(request: HttpRequest) -> int | None:
        raw_value = request.META.get("CONTENT_LENGTH")
        if raw_value in (None, ""):
            return None
        try:
            return max(0, int(raw_value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _too_large_response() -> JsonResponse:
        return JsonResponse(
            {
                "error_code": "UPLOAD_TOO_LARGE",
                "message": "Upload request exceeds the endpoint byte limit.",
            },
            status=413,
        )
