"""HTTP views for the project poster API.

Lives next to the legacy ``views.py`` (which still serves the old
``/api/generate/poster/`` endpoint) but is wired under ``/api/projects/...``
to follow the dashboard convention.

The thin layer here:
  1. resolves the user from the existing ``token_user`` auth scheme,
  2. validates input via DRF serializers,
  3. delegates to ``facade.py``,
  4. translates ``PosterError`` into the canonical response shape.
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.movie.poster import facade
from w_craft_back.movie.poster.errors import (
    InvalidPosterFormat,
    InvalidPosterStyle,
    PosterError,
    PromptRequired,
    PromptTooLong,
)
from w_craft_back.movie.poster.serializers import (
    PROMPT_MAX_LENGTH,
    PosterGenerateSerializer,
    PosterSelectSerializer,
)
from w_craft_back.movie.project.dashboard_views import (
    _resolve_user,
    _unauthorized,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Error envelope
# --------------------------------------------------------------------------- #

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


# Map specific serializer field errors to stable codes the FE can branch on.
def _coerce_generate_errors(errors: dict) -> Response:
    prompt_errs = errors.get("prompt") or []
    style_errs = errors.get("style") or []
    format_errs = errors.get("format") or []

    if prompt_errs:
        msg = str(prompt_errs[0])
        if "max" in msg or f"{PROMPT_MAX_LENGTH}" in msg:
            return _error_response(PromptTooLong(msg, errors=errors))
        return _error_response(PromptRequired(msg, errors=errors))
    if style_errs:
        return _error_response(InvalidPosterStyle(str(style_errs[0]), errors=errors))
    if format_errs:
        return _error_response(InvalidPosterFormat(str(format_errs[0]), errors=errors))
    return _validation_error(errors)


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #

class _AuthedView(APIView):
    """Resolve user once, return 401 if missing."""

    def _user(self, request):
        user = _resolve_user(request)
        if user is None:
            return None, _unauthorized()
        return user, None


class ProjectPosterView(_AuthedView):
    """``GET /api/projects/<project_id>/poster/``"""

    def get(self, request, project_id: int):
        user, err = self._user(request)
        if err:
            return err
        try:
            data = facade.get_project_poster(user, project_id, request=request)
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)


class ProjectPosterGenerateView(_AuthedView):
    """``POST /api/projects/<project_id>/poster/generate/``"""

    def post(self, request, project_id: int):
        user, err = self._user(request)
        if err:
            return err

        serializer = PosterGenerateSerializer(data=request.data)
        if not serializer.is_valid():
            return _coerce_generate_errors(serializer.errors)
        data = serializer.validated_data

        try:
            payload = facade.generate_poster(
                user,
                project_id,
                prompt=data["prompt"],
                style=data["style"],
                format=data["format"],
                reference_image_url=data.get("reference_image_url") or "",
                reference_image_asset_id=data.get("reference_image_asset_id"),
                request=request,
            )
        except PosterError as exc:
            return _error_response(exc)
        return Response(payload, status=status.HTTP_201_CREATED)


class ProjectPosterJobDetailView(_AuthedView):
    """``GET /api/projects/<project_id>/poster/jobs/<job_id>/``"""

    def get(self, request, project_id: int, job_id: int):
        user, err = self._user(request)
        if err:
            return err
        try:
            data = facade.get_poster_job(user, project_id, job_id, request=request)
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)


class ProjectPosterVariantsView(_AuthedView):
    """``GET /api/projects/<project_id>/poster/variants/?limit=8``"""

    def get(self, request, project_id: int):
        user, err = self._user(request)
        if err:
            return err

        try:
            limit = int(request.query_params.get("limit", facade.DEFAULT_VARIANT_LIMIT))
        except (TypeError, ValueError):
            limit = facade.DEFAULT_VARIANT_LIMIT

        try:
            data = facade.get_poster_variants(
                user, project_id, limit=limit, request=request
            )
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)


class ProjectPosterSelectView(_AuthedView):
    """``PATCH /api/projects/<project_id>/poster/select/``"""

    def patch(self, request, project_id: int):
        user, err = self._user(request)
        if err:
            return err

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
    """``DELETE /api/projects/<project_id>/poster/variants/<variant_id>/``"""

    def delete(self, request, project_id: int, variant_id: int):
        user, err = self._user(request)
        if err:
            return err
        try:
            data = facade.delete_poster_variant(
                user, project_id, variant_id, request=request
            )
        except PosterError as exc:
            return _error_response(exc)
        return Response(data, status=status.HTTP_200_OK)
