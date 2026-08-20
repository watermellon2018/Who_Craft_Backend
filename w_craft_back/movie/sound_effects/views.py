"""Header-authenticated HTTP views for project Sound Effects."""

from __future__ import annotations

import logging
from functools import wraps

from rest_framework import status
from rest_framework.exceptions import APIException, NotAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.movie.sound_effects import services
from w_craft_back.movie.sound_effects.errors import SoundEffectError
from w_craft_back.movie.sound_effects.serializers import (
    ApplyVariantSerializer,
    GenerationCreateSerializer,
)


logger = logging.getLogger(__name__)


def handle_sound_effect_errors(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except SoundEffectError as error:
            payload = {
                "code": error.code,
                "detail": error.detail,
                "retryable": error.retryable,
            }
            if error.errors is not None:
                payload["errors"] = error.errors
            return Response(payload, status=error.http_status)
        except APIException:
            raise
        except Exception:
            logger.exception("Unexpected Sound Effects API failure")
            return Response(
                {
                    "code": "SOUND_EFFECT_INTERNAL_ERROR",
                    "detail": "Sound Effects is temporarily unavailable.",
                    "retryable": True,
                },
                status=500,
            )

    return wrapped


class SoundEffectAuthedView(APIView):
    @staticmethod
    def actor(request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            raise NotAuthenticated("X-User-Token header is required.")
        return user


class SoundEffectCapabilitiesView(SoundEffectAuthedView):
    @handle_sound_effect_errors
    def get(self, request, project_id: int):
        return Response(
            services.get_capabilities(
                actor=self.actor(request),
                project_id=project_id,
            )
        )


class SoundEffectCollectionView(SoundEffectAuthedView):
    @handle_sound_effect_errors
    def get(self, request, project_id: int):
        return Response(
            services.list_effects(
                actor=self.actor(request),
                project_id=project_id,
                request=request,
            )
        )


class SoundEffectGenerationJobsView(SoundEffectAuthedView):
    @handle_sound_effect_errors
    def get(self, request, project_id: int):
        return Response(
            services.list_jobs(
                actor=self.actor(request),
                project_id=project_id,
                request=request,
            )
        )

    @handle_sound_effect_errors
    def post(self, request, project_id: int):
        key = str(request.headers.get("Idempotency-Key") or "").strip()
        if not key:
            raise SoundEffectError(
                "Idempotency-Key header is required.",
                code="SOUND_EFFECT_IDEMPOTENCY_REQUIRED",
                http_status=400,
            )
        serializer = GenerationCreateSerializer(data=request.data)
        if not serializer.is_valid():
            raise SoundEffectError(
                "Validation failed.",
                code="SOUND_EFFECT_VALIDATION_ERROR",
                errors=serializer.errors,
            )
        payload = services.enqueue_job(
            actor=self.actor(request),
            project_id=project_id,
            data=serializer.validated_data,
            idempotency_key=key,
        )
        return Response(payload, status=status.HTTP_202_ACCEPTED)


class SoundEffectGenerationJobDetailView(SoundEffectAuthedView):
    @handle_sound_effect_errors
    def get(self, request, project_id: int, job_id):
        return Response(
            services.get_job(
                actor=self.actor(request),
                project_id=project_id,
                job_id=job_id,
                request=request,
            )
        )


class SoundEffectGenerationJobCancellationView(SoundEffectAuthedView):
    @handle_sound_effect_errors
    def post(self, request, project_id: int, job_id):
        return Response(
            services.cancel_job(
                actor=self.actor(request),
                project_id=project_id,
                job_id=job_id,
                request=request,
            ),
            status=status.HTTP_202_ACCEPTED,
        )


class SoundEffectGenerationJobRetryView(SoundEffectAuthedView):
    @handle_sound_effect_errors
    def post(self, request, project_id: int, job_id):
        return Response(
            services.retry_job(
                actor=self.actor(request),
                project_id=project_id,
                job_id=job_id,
            ),
            status=status.HTTP_202_ACCEPTED,
        )


class SoundEffectVariantApplyView(SoundEffectAuthedView):
    @handle_sound_effect_errors
    def post(self, request, project_id: int, job_id, variant_id):
        serializer = ApplyVariantSerializer(data=request.data)
        if not serializer.is_valid():
            raise SoundEffectError(
                "Validation failed.",
                code="SOUND_EFFECT_VALIDATION_ERROR",
                errors=serializer.errors,
            )
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
            status=(status.HTTP_201_CREATED if created else status.HTTP_200_OK),
        )


class SoundEffectAssignmentsView(SoundEffectAuthedView):
    @handle_sound_effect_errors
    def get(self, request, project_id: int):
        return Response(
            services.list_assignments(
                actor=self.actor(request),
                project_id=project_id,
            )
        )
