"""Header-only, project-scoped HTTP views for Music Studio."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.movie.music import services
from w_craft_back.movie.music.errors import (
    IdempotencyRequired,
    MusicError,
    ReferenceRightsRequired,
    validation_error,
)
from w_craft_back.movie.music.serializers import (
    ApplyVariantSerializer,
    ArchiveTrackSerializer,
    AssignmentReplaceSerializer,
    GenerationCreateSerializer,
    LegacyMetadataTrackSerializer,
    ReferenceUploadSerializer,
    TrackPatchSerializer,
)

logger = logging.getLogger(__name__)


def _error_response(error: MusicError) -> Response:
    payload: dict[str, Any] = {
        "code": error.code,
        "detail": error.detail,
        "retryable": error.retryable,
    }
    if error.errors is not None:
        payload["errors"] = error.errors
    if error.current_version is not None:
        payload["currentVersion"] = error.current_version
    return Response(payload, status=error.http_status)


def handle_music_errors(handler: Callable) -> Callable:
    """Map service errors without exposing provider or storage internals."""

    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except MusicError as error:
            return _error_response(error)
        except APIException:
            raise
        except Exception:
            logger.exception("Unexpected Music Studio API failure")
            return Response(
                {
                    "code": "MUSIC_INTERNAL_ERROR",
                    "detail": "Music Studio is temporarily unavailable.",
                    "retryable": True,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    return wrapped


class MusicAuthedView(APIView):
    """Music endpoints deliberately never accept legacy body credentials."""

    @staticmethod
    def actor(request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            from rest_framework.exceptions import NotAuthenticated

            raise NotAuthenticated("X-User-Token header is required.")
        return user


class MusicCollectionView(MusicAuthedView):
    @handle_music_errors
    def get(self, request, project_id: int):
        payload = services.list_tracks(
            actor=self.actor(request),
            project_id=project_id,
            request=request,
            query=request.query_params.get("q", ""),
            status_filter=request.query_params.get("status", "active"),
            limit=request.query_params.get("limit", 30),
            offset=request.query_params.get("offset", 0),
        )
        return Response(payload)

    @handle_music_errors
    def post(self, request, project_id: int):
        """Preserve the existing metadata-only POST request and response."""

        serializer = LegacyMetadataTrackSerializer(data=request.data)
        if not serializer.is_valid():
            raise validation_error(serializer.errors)
        payload = services.create_legacy_metadata_track(
            actor=self.actor(request),
            project_id=project_id,
            data=serializer.validated_data,
        )
        return Response(payload, status=status.HTTP_201_CREATED)


class MusicCapabilitiesView(MusicAuthedView):
    @handle_music_errors
    def get(self, request, project_id: int):
        return Response(
            services.get_capabilities(
                actor=self.actor(request),
                project_id=project_id,
            )
        )


class MusicSceneOptionsView(MusicAuthedView):
    @handle_music_errors
    def get(self, request, project_id: int):
        return Response(
            services.list_scene_options(
                actor=self.actor(request),
                project_id=project_id,
                query=request.query_params.get("q", ""),
                act=request.query_params.get("act"),
                limit=request.query_params.get("limit", 20),
                scene_id=request.query_params.get("sceneId"),
            )
        )


class MusicReferenceAssetsView(MusicAuthedView):
    parser_classes = (MultiPartParser, FormParser)

    @handle_music_errors
    def post(self, request, project_id: int):
        serializer = ReferenceUploadSerializer(data=request.data)
        if not serializer.is_valid():
            if "rightsConfirmed" in serializer.errors:
                raise ReferenceRightsRequired(
                    "Usage rights must be confirmed.",
                    errors=serializer.errors,
                )
            raise validation_error(serializer.errors)
        payload = services.create_reference_asset(
            actor=self.actor(request),
            project_id=project_id,
            upload=serializer.validated_data["file"],
            rights_statement_version=serializer.validated_data[
                "rightsStatementVersion"
            ],
            request=request,
        )
        return Response(payload, status=status.HTTP_201_CREATED)


class MusicReferenceAssetDetailView(MusicAuthedView):
    @handle_music_errors
    def delete(self, request, project_id: int, asset_id):
        services.delete_reference_asset(
            actor=self.actor(request),
            project_id=project_id,
            asset_id=asset_id,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class MusicTrackDetailView(MusicAuthedView):
    @handle_music_errors
    def get(self, request, project_id: int, track_id: int):
        return Response(
            services.get_track(
                actor=self.actor(request),
                project_id=project_id,
                track_id=track_id,
                request=request,
            )
        )

    @handle_music_errors
    def patch(self, request, project_id: int, track_id: int):
        serializer = TrackPatchSerializer(data=request.data)
        if not serializer.is_valid():
            raise validation_error(serializer.errors)
        return Response(
            services.update_track(
                actor=self.actor(request),
                project_id=project_id,
                track_id=track_id,
                data=serializer.validated_data,
                request=request,
            )
        )


class MusicTrackArchiveView(MusicAuthedView):
    @handle_music_errors
    def post(self, request, project_id: int, track_id: int):
        serializer = ArchiveTrackSerializer(data=request.data)
        if not serializer.is_valid():
            raise validation_error(serializer.errors)
        return Response(
            services.archive_track(
                actor=self.actor(request),
                project_id=project_id,
                track_id=track_id,
                expected_version=serializer.validated_data[
                    "expectedTrackVersion"
                ],
                request=request,
            )
        )


class MusicTrackAssignmentsView(MusicAuthedView):
    @handle_music_errors
    def get(self, request, project_id: int, track_id: int):
        return Response(
            services.get_assignments(
                actor=self.actor(request),
                project_id=project_id,
                track_id=track_id,
            )
        )

    @handle_music_errors
    def put(self, request, project_id: int, track_id: int):
        serializer = AssignmentReplaceSerializer(data=request.data)
        if not serializer.is_valid():
            raise validation_error(serializer.errors)
        return Response(
            services.replace_assignments(
                actor=self.actor(request),
                project_id=project_id,
                track_id=track_id,
                data=serializer.validated_data,
            )
        )


class MusicGenerationJobsView(MusicAuthedView):
    @handle_music_errors
    def get(self, request, project_id: int):
        return Response(
            services.list_jobs(
                actor=self.actor(request),
                project_id=project_id,
                request=request,
                limit=request.query_params.get("limit", 30),
                offset=request.query_params.get("offset", 0),
                status_filter=request.query_params.get("status", ""),
            )
        )

    @handle_music_errors
    def post(self, request, project_id: int):
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if not key:
            raise IdempotencyRequired("Idempotency-Key header is required.")
        serializer = GenerationCreateSerializer(data=request.data)
        if not serializer.is_valid():
            raise validation_error(serializer.errors)
        payload = services.enqueue_job(
            actor=self.actor(request),
            project_id=project_id,
            data=serializer.validated_data,
            idempotency_key=key,
        )
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class MusicGenerationJobDetailView(MusicAuthedView):
    @handle_music_errors
    def get(self, request, project_id: int, job_id):
        return Response(
            services.get_job(
                actor=self.actor(request),
                project_id=project_id,
                job_id=job_id,
                request=request,
            )
        )


class MusicGenerationJobCancellationView(MusicAuthedView):
    @handle_music_errors
    def post(self, request, project_id: int, job_id):
        payload = services.cancel_job(
            actor=self.actor(request),
            project_id=project_id,
            job_id=job_id,
            request=request,
        )
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class MusicGenerationJobRetryView(MusicAuthedView):
    @handle_music_errors
    def post(self, request, project_id: int, job_id):
        payload = services.retry_job(
            actor=self.actor(request),
            project_id=project_id,
            job_id=job_id,
        )
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class MusicVariantApplyView(MusicAuthedView):
    @handle_music_errors
    def post(self, request, project_id: int, job_id, variant_id):
        serializer = ApplyVariantSerializer(data=request.data)
        if not serializer.is_valid():
            raise validation_error(serializer.errors)
        payload, created = services.apply_variant(
            actor=self.actor(request),
            project_id=project_id,
            job_id=job_id,
            variant_id=variant_id,
            data=serializer.validated_data,
            request=request,
        )
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
