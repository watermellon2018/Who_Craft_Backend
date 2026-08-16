"""HTTP views for the authenticated, project-scoped poster API."""

from __future__ import annotations

from django.core.files.uploadhandler import FileUploadHandler, StopUpload

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.movie.poster import facade
from w_craft_back.movie.poster.errors import (
    InvalidPosterFormat,
    InvalidPosterStyle,
    PosterError,
    PosterImageTooLarge,
    PromptRequired,
    PromptTooLong,
)
from w_craft_back.movie.poster.generation_guard import (
    max_input_bytes,
    normalize_idempotency_key,
)
from w_craft_back.movie.poster.serializers import (
    PROMPT_MAX_LENGTH,
    PosterEditSerializer,
    PosterGenerateSerializer,
    PosterSelectSerializer,
)
from w_craft_back.movie.project.dashboard_views import _unauthorized


def _error_response(err: PosterError) -> Response:
    payload: dict = {"detail": err.message or err.code, "code": err.code}
    if err.errors is not None:
        payload["errors"] = err.errors
    return Response(payload, status=err.http_status)


def _validation_error(errors: dict, code: str = "VALIDATION_ERROR") -> Response:
    return Response(
        {"detail": "validation error", "code": code, "errors": errors},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _coerce_generate_errors(errors: dict) -> Response:
    prompt_errors = errors.get("prompt") or []
    style_errors = errors.get("style") or []
    format_errors = errors.get("format") or []
    if prompt_errors:
        message = str(prompt_errors[0])
        if "max" in message or f"{PROMPT_MAX_LENGTH}" in message:
            return _error_response(PromptTooLong(message, errors=errors))
        return _error_response(PromptRequired(message, errors=errors))
    if style_errors:
        return _error_response(
            InvalidPosterStyle(str(style_errors[0]), errors=errors)
        )
    if format_errors:
        return _error_response(
            InvalidPosterFormat(str(format_errors[0]), errors=errors)
        )
    return _validation_error(errors)


class _PosterReferenceUploadLimitHandler(FileUploadHandler):
    """Stop streaming a poster reference once it exceeds the byte limit."""

    def __init__(self, request=None):
        super().__init__(request)
        self.received_bytes = 0

    def receive_data_chunk(self, raw_data, start):
        self.received_bytes += len(raw_data)
        if self.received_bytes > max_input_bytes():
            setattr(self.request, "_poster_upload_exceeded", True)
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None


def _prepare_reference_upload(request) -> None:
    """Reject declared oversized requests before parsing and cap chunked files."""
    raw_request = getattr(request, "_request", request)
    content_length = raw_request.META.get("CONTENT_LENGTH")
    if content_length:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            declared_size = 0
        multipart_overhead = 64 * 1024
        if declared_size > max_input_bytes() + multipart_overhead:
            raise PosterImageTooLarge("Poster request exceeds the byte limit")
    raw_request.upload_handlers.insert(
        0,
        _PosterReferenceUploadLimitHandler(raw_request),
    )


def _read_reference(uploaded_file) -> bytes | None:
    if uploaded_file is None:
        return None
    normalized = getattr(uploaded_file, "_storage_gateway_normalized", None)
    if normalized is None:
        from w_craft_back.movie.poster.file_validation import (
            ReferenceImageValidationError,
            validate_reference_image,
        )

        try:
            normalized = validate_reference_image(uploaded_file)
        except ReferenceImageValidationError as exc:
            raise PosterError(exc.message, code=exc.code) from exc
    if normalized is None:
        return None
    return normalized.data


class _AuthedView(APIView):
    """Resolve the custom token once and reject anonymous requests."""

    def _user(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None, _unauthorized()
        return user, None

    @staticmethod
    def _idempotency_key(request):
        return normalize_idempotency_key(request.headers.get("Idempotency-Key"))


class ProjectPosterView(_AuthedView):
    def get(self, request, project_id: int):
        user, error = self._user(request)
        if error:
            return error
        try:
            data = facade.get_project_poster(user, project_id, request=request)
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)


class ProjectPosterGenerateView(_AuthedView):
    """POST-only paid poster generation endpoint."""

    def post(self, request, project_id: int):
        user, error = self._user(request)
        if error:
            return error
        try:
            idempotency_key = self._idempotency_key(request)
        except PosterError as exc:
            return _error_response(exc)

        try:
            _prepare_reference_upload(request)
        except PosterError as exc:
            return _error_response(exc)

        request_data = request.data
        raw_request = getattr(request, "_request", request)
        if getattr(raw_request, "_poster_upload_exceeded", False):
            return _error_response(
                PosterImageTooLarge("Reference image exceeds the byte limit")
            )

        serializer = PosterGenerateSerializer(data=request_data)
        if not serializer.is_valid():
            return _coerce_generate_errors(serializer.errors)
        data = serializer.validated_data
        reference = data.get("reference_image")

        try:
            reference_bytes = _read_reference(reference)
            payload = facade.generate_poster(
                user,
                project_id,
                prompt=data["prompt"],
                style=data["style"],
                format=data["format"],
                idempotency_key=idempotency_key,
                reference_image_bytes=reference_bytes,
                reference_mime_type=(
                    getattr(
                        getattr(reference, "_storage_gateway_normalized", None),
                        "mime_type",
                        "image/png",
                    )
                ),
                reference_image_url=data.get("reference_image_url") or "",
                reference_image_asset_id=data.get("reference_image_asset_id"),
                image_model=data.get("image_model"),
                routing_mode=data["routing_mode"],
                request=request,
                execute_immediately=False,
            )
        except PosterError as exc:
            return _error_response(exc)

        return Response(payload, status=status.HTTP_202_ACCEPTED)


class ProjectPosterEditView(_AuthedView):
    """POST-only edit operation using a project-owned source variant."""

    def post(self, request, project_id: int):
        user, error = self._user(request)
        if error:
            return error
        try:
            idempotency_key = self._idempotency_key(request)
        except PosterError as exc:
            return _error_response(exc)

        serializer = PosterEditSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data
        try:
            payload = facade.edit_poster(
                user,
                project_id,
                source_variant_id=data["source_variant_id"],
                instruction=data["instruction"],
                idempotency_key=idempotency_key,
                image_model=data.get("image_model"),
                routing_mode=data["routing_mode"],
                request=request,
                execute_immediately=False,
            )
        except PosterError as exc:
            return _error_response(exc)

        return Response(payload, status=status.HTTP_202_ACCEPTED)


class ProjectPosterJobDetailView(_AuthedView):
    def get(self, request, project_id: int, job_id: int):
        user, error = self._user(request)
        if error:
            return error
        try:
            data = facade.get_poster_job(user, project_id, job_id, request=request)
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)


class ProjectPosterJobsView(_AuthedView):
    def get(self, request, project_id: int):
        user, error = self._user(request)
        if error:
            return error
        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        try:
            data = facade.get_poster_jobs(
                user,
                project_id,
                limit=limit,
                request=request,
            )
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)


class ProjectPosterJobRetryView(_AuthedView):
    def post(self, request, project_id: int, job_id: int):
        user, error = self._user(request)
        if error:
            return error
        try:
            data = facade.retry_poster_generation(
                user,
                project_id,
                job_id,
                request=request,
            )
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_202_ACCEPTED)


class ProjectPosterJobCancellationView(_AuthedView):
    def post(self, request, project_id: int, job_id: int):
        user, error = self._user(request)
        if error:
            return error
        try:
            data = facade.cancel_poster_generation(
                user,
                project_id,
                job_id,
                request=request,
            )
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_202_ACCEPTED)


class ProjectPosterVariantsView(_AuthedView):
    def get(self, request, project_id: int):
        user, error = self._user(request)
        if error:
            return error
        try:
            limit = int(request.query_params.get("limit", facade.DEFAULT_VARIANT_LIMIT))
        except (TypeError, ValueError):
            limit = facade.DEFAULT_VARIANT_LIMIT
        try:
            data = facade.get_poster_variants(
                user,
                project_id,
                limit=limit,
                request=request,
            )
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)


class ProjectPosterSelectView(_AuthedView):
    def patch(self, request, project_id: int):
        user, error = self._user(request)
        if error:
            return error
        serializer = PosterSelectSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        try:
            data = facade.select_poster_variant(
                user,
                project_id,
                serializer.validated_data["variant_id"],
                request=request,
            )
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)


class ProjectPosterVariantDeleteView(_AuthedView):
    def delete(self, request, project_id: int, variant_id: int):
        user, error = self._user(request)
        if error:
            return error
        try:
            data = facade.delete_poster_variant(
                user,
                project_id,
                variant_id,
                request=request,
            )
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)
