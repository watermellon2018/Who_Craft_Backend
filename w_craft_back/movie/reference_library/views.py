"""Project-scoped REST views for the Reference Library."""

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

from w_craft_back.movie.reference_library import services
from w_craft_back.movie.reference_library.errors import ReferenceError, validation_error
from w_craft_back.movie.reference_library.serializers import (
    ExpectedReferenceVersionSerializer,
    GenerationCreateSerializer,
    ReferenceCreateSerializer,
    ReferencePatchSerializer,
    ReferenceUploadSerializer,
    SceneReferenceReplaceSerializer,
)


logger = logging.getLogger(__name__)


def _error_response(error: ReferenceError) -> Response:
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


def handle_reference_errors(handler: Callable) -> Callable:
    """Map only stable public errors and hide implementation details."""

    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except ReferenceError as error:
            return _error_response(error)
        except APIException:
            raise
        except Exception:
            logger.exception("Unexpected Reference Library API failure")
            return Response(
                {
                    "code": "REFERENCE_INTERNAL_ERROR",
                    "detail": "Reference Library is temporarily unavailable.",
                    "retryable": True,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    return wrapped


def _validated(serializer_class, data):
    serializer = serializer_class(data=data)
    if not serializer.is_valid():
        raise validation_error(serializer.errors)
    return serializer.validated_data


class ReferenceAuthedView(APIView):
    @staticmethod
    def actor(request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            raise ReferenceError(
                "X-User-Token header is required.",
                code="AUTH_REQUIRED",
                http_status=401,
            )
        return user


class ReferenceCollectionView(ReferenceAuthedView):
    @handle_reference_errors
    def get(self, request, project_id: int):
        return Response(
            services.list_references(
                actor=self.actor(request),
                project_id=project_id,
                request=request,
                params=request.query_params,
            )
        )

    @handle_reference_errors
    def post(self, request, project_id: int):
        data = _validated(ReferenceCreateSerializer, request.data)
        payload = services.create_reference(
            actor=self.actor(request),
            project_id=project_id,
            data=data,
            request=request,
        )
        return Response(payload, status=status.HTTP_201_CREATED)


class ReferenceCapabilitiesView(ReferenceAuthedView):
    @handle_reference_errors
    def get(self, request, project_id: int):
        return Response(
            services.get_capabilities(
                actor=self.actor(request),
                project_id=project_id,
            )
        )


class ReferenceLinkOptionsView(ReferenceAuthedView):
    @handle_reference_errors
    def get(self, request, project_id: int):
        return Response(
            services.get_link_options(
                actor=self.actor(request),
                project_id=project_id,
            )
        )


class ReferenceDetailView(ReferenceAuthedView):
    @handle_reference_errors
    def get(self, request, project_id: int, reference_id):
        return Response(
            services.get_reference(
                actor=self.actor(request),
                project_id=project_id,
                reference_id=reference_id,
                request=request,
            )
        )

    @handle_reference_errors
    def patch(self, request, project_id: int, reference_id):
        data = _validated(ReferencePatchSerializer, request.data)
        return Response(
            services.update_reference(
                actor=self.actor(request),
                project_id=project_id,
                reference_id=reference_id,
                data=data,
                request=request,
            )
        )


class _ReferenceArchiveBase(ReferenceAuthedView):
    archived = True

    @handle_reference_errors
    def post(self, request, project_id: int, reference_id):
        data = _validated(ExpectedReferenceVersionSerializer, request.data)
        return Response(
            services.set_archived(
                actor=self.actor(request),
                project_id=project_id,
                reference_id=reference_id,
                expected_version=data["expectedReferenceVersion"],
                archived=self.archived,
                request=request,
            )
        )


class ReferenceArchiveView(_ReferenceArchiveBase):
    archived = True


class ReferenceRestoreView(_ReferenceArchiveBase):
    archived = False


class ReferenceVersionsView(ReferenceAuthedView):
    @handle_reference_errors
    def get(self, request, project_id: int, reference_id):
        return Response(
            services.list_versions(
                actor=self.actor(request),
                project_id=project_id,
                reference_id=reference_id,
                request=request,
            )
        )


class ReferenceVersionUploadView(ReferenceAuthedView):
    parser_classes = (MultiPartParser, FormParser)

    @handle_reference_errors
    def post(self, request, project_id: int, reference_id):
        serializer = ReferenceUploadSerializer(data=request.data)
        if not serializer.is_valid():
            if (
                "rightsConfirmed" in serializer.errors
                or "rightsStatementVersion" in serializer.errors
            ):
                raise ReferenceError(
                    "Usage rights must be confirmed.",
                    code="REFERENCE_UPLOAD_RIGHTS_REQUIRED",
                    errors=serializer.errors,
                )
            raise validation_error(serializer.errors)
        data = serializer.validated_data
        payload = services.upload_version(
            actor=self.actor(request),
            project_id=project_id,
            reference_id=reference_id,
            upload=data["file"],
            expected_version=data["expectedReferenceVersion"],
            rights_statement_version=data["rightsStatementVersion"],
            request=request,
        )
        return Response(payload, status=status.HTTP_201_CREATED)


class ReferenceGenerationJobsView(ReferenceAuthedView):
    @handle_reference_errors
    def get(self, request, project_id: int, reference_id):
        return Response(
            services.list_jobs(
                actor=self.actor(request),
                project_id=project_id,
                reference_id=reference_id,
                request=request,
            )
        )

    @handle_reference_errors
    def post(self, request, project_id: int, reference_id):
        key = (request.headers.get("Idempotency-Key") or "").strip()
        data = _validated(GenerationCreateSerializer, request.data)
        payload, _created = services.enqueue_job(
            actor=self.actor(request),
            project_id=project_id,
            reference_id=reference_id,
            data=data,
            idempotency_key=key,
            request=request,
        )
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class ReferenceGenerationJobDetailView(ReferenceAuthedView):
    @handle_reference_errors
    def get(self, request, project_id: int, reference_id, job_id):
        return Response(
            services.get_job(
                actor=self.actor(request),
                project_id=project_id,
                reference_id=reference_id,
                job_id=job_id,
                request=request,
            )
        )


class ReferenceGenerationJobCancellationView(ReferenceAuthedView):
    @handle_reference_errors
    def post(self, request, project_id: int, reference_id, job_id):
        return Response(
            services.cancel_job_service(
                actor=self.actor(request),
                project_id=project_id,
                reference_id=reference_id,
                job_id=job_id,
                request=request,
            ),
            status=status.HTTP_202_ACCEPTED,
        )


class ReferenceGenerationJobRetryView(ReferenceAuthedView):
    @handle_reference_errors
    def post(self, request, project_id: int, reference_id, job_id):
        return Response(
            services.retry_job_service(
                actor=self.actor(request),
                project_id=project_id,
                reference_id=reference_id,
                job_id=job_id,
                request=request,
            ),
            status=status.HTTP_202_ACCEPTED,
        )


class ReferenceVariantApplyView(ReferenceAuthedView):
    @handle_reference_errors
    def post(self, request, project_id: int, reference_id, job_id, variant_id):
        data = _validated(ExpectedReferenceVersionSerializer, request.data)
        payload, created = services.apply_variant(
            actor=self.actor(request),
            project_id=project_id,
            reference_id=reference_id,
            job_id=job_id,
            variant_id=variant_id,
            expected_version=data["expectedReferenceVersion"],
            request=request,
        )
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SceneReferencesView(ReferenceAuthedView):
    @handle_reference_errors
    def get(self, request, project_id: int, scene_id: int):
        return Response(
            services.get_scene_references(
                actor=self.actor(request),
                project_id=project_id,
                scene_id=scene_id,
                request=request,
            )
        )

    @handle_reference_errors
    def put(self, request, project_id: int, scene_id: int):
        data = _validated(SceneReferenceReplaceSerializer, request.data)
        return Response(
            services.replace_scene_references(
                actor=self.actor(request),
                project_id=project_id,
                scene_id=scene_id,
                expected_scene_version=data["expectedSceneVersion"],
                items=data["items"],
                request=request,
            )
        )
