"""Project-scoped REST endpoints for Storyboard workspaces."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from w_craft_back.movie.project import policy
from w_craft_back.movie.storyboard import (
    editor_drafts, editor_frames, generation, services, shot_list_jobs,
)
from w_craft_back.movie.storyboard.editor_drafts import EditorDraftPutSerializer
from w_craft_back.movie.storyboard.errors import (
    StoryboardError,
    validation_error,
)
from w_craft_back.movie.storyboard.serializers import (
    CameraIntentSerializer,
    GenerateKeyframeSerializer,
    GenerationReferencesReplaceSerializer,
    KeyframeCreateSerializer,
    KeyframePatchSerializer,
    ShotCreateSerializer,
    ShotListSuggestSerializer,
    ShotListJobCreateSerializer,
    ShotListJobApplySerializer,
    ShotPatchSerializer,
    ShotReorderSerializer,
    TransitionPatchSerializer,
)
from w_craft_back.movie.storyboard.shot_list import AIShotListService
from w_craft_back.movie.storyboard.source import source_from_scene


logger = logging.getLogger(__name__)


class StoryboardShotListRateThrottle(UserRateThrottle):
    """Use the endpoint rate without depending on a DRF scope dictionary."""

    scope = "storyboard_shot_list"

    def get_rate(self) -> str:
        return str(
            getattr(settings, "STORYBOARD_SHOT_LIST_THROTTLE_RATE", "10/min")
        ).strip() or "10/min"


def error_response(error: StoryboardError) -> Response:
    payload: dict[str, Any] = {
        "code": error.code,
        "detail": error.detail,
        "retryable": error.retryable,
    }
    if error.errors is not None:
        payload["errors"] = error.errors
    return Response(payload, status=error.http_status)


def handle_storyboard_errors(handler: Callable) -> Callable:
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except StoryboardError as error:
            return error_response(error)
        except ValidationError as error:
            return error_response(
                validation_error(
                    getattr(error, "message_dict", {"detail": error.messages})
                )
            )
        except APIException:
            raise
        except Exception:
            logger.exception("Unexpected Storyboard API failure")
            return Response(
                {
                    "code": "STORYBOARD_INTERNAL_ERROR",
                    "detail": "Storyboard is temporarily unavailable.",
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


class StoryboardAuthedView(APIView):
    @staticmethod
    def actor(request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            raise StoryboardError(
                "X-User-Token header is required.",
                code="AUTH_REQUIRED",
                http_status=401,
            )
        return user


class SceneStoryboardListView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int):
        return Response(
            services.list_scene_storyboards(
                actor=self.actor(request),
                project_id=project_id,
            )
        )


class StoryboardEditorDraftListView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int):
        return Response(editor_drafts.list_editor_drafts(
            actor=self.actor(request), project_id=project_id,
        ))


class SceneStoryboardEditorDraftView(StoryboardAuthedView):
    @handle_storyboard_errors
    def put(self, request, project_id: int, scene_id: int):
        actor = self.actor(request)
        data = _validated(EditorDraftPutSerializer, request.data)
        return Response(editor_drafts.save_editor_draft(
            actor=actor, project_id=project_id, scene_id=scene_id,
            expected_revision=data["expectedRevision"],
            mutation_id=data["mutationId"], payload=data["payload"],
        ))


class SceneStoryboardEditorFrameOptionsView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int, scene_id: int):
        return Response(editor_frames.frame_options(
            actor=self.actor(request), project_id=project_id, scene_id=scene_id,
        ))


class SceneStoryboardEditorFrameJobsView(StoryboardAuthedView):
    def get_throttles(self):
        if self.request.method == "POST":
            return [StoryboardShotListRateThrottle()]
        return []

    @handle_storyboard_errors
    def get(self, request, project_id: int, scene_id: int):
        return Response(editor_frames.list_frame_jobs(
            actor=self.actor(request), project_id=project_id,
            scene_id=scene_id, request=request,
        ))

    @handle_storyboard_errors
    def post(self, request, project_id: int, scene_id: int):
        data = _validated(editor_frames.EditorFrameCreateSerializer, request.data)
        return Response(editor_frames.enqueue_frame(
            actor=self.actor(request), project_id=project_id, scene_id=scene_id,
            data=data, request=request,
        ), status=202)


def _shot_list_language(actor: Any, requested: str | None) -> str:
    """Explicit UI language wins; otherwise honor the saved interface locale."""
    if requested is not None:
        return requested
    language = getattr(getattr(actor, "profile", None), "language", "ru")
    return language if language in ("ru", "en") else "ru"


class SceneStoryboardDetailView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int, scene_id: int):
        return Response(
            services.get_scene_storyboard(
                actor=self.actor(request),
                project_id=project_id,
                scene_id=scene_id,
                request=request,
            )
        )

    @handle_storyboard_errors
    def post(self, request, project_id: int, scene_id: int):
        payload, created = services.initialize_storyboard(
            actor=self.actor(request),
            project_id=project_id,
            scene_id=scene_id,
            request=request,
        )
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SceneStoryboardShotListView(StoryboardAuthedView):
    throttle_classes = [UserRateThrottle]

    def get_throttles(self):
        """Reserve the stricter generation limit for POST requests only."""

        if self.request.method == "POST":
            return [StoryboardShotListRateThrottle()]
        return super().get_throttles()

    @handle_storyboard_errors
    def get(self, request, project_id: int, scene_id: int):
        data = _validated(ShotListSuggestSerializer, request.query_params)
        actor = self.actor(request)
        project = services._require_project(
            actor=actor,
            project_id=project_id,
            action=policy.Action.RUN_GENERATION,
        )
        scene = services._scene(project, scene_id)
        context = services.SceneStoryboardContextService.build(scene)
        return Response(AIShotListService.options(
            context=context, max_shots=16, source=source_from_scene(scene),
            language=_shot_list_language(actor, data.get("language")),
        ))

    @handle_storyboard_errors
    def post(self, request, project_id: int, scene_id: int):
        data = _validated(ShotListSuggestSerializer, request.data)
        actor = self.actor(request)
        project = services._require_project(
            actor=actor,
            project_id=project_id,
            action=policy.Action.RUN_GENERATION,
        )
        scene = services._scene(project, scene_id)
        context = services.SceneStoryboardContextService.build(scene)
        return Response(
            AIShotListService(model=data.get("model")).suggest(
                context=context,
                max_shots=data["maxShots"],
                source=source_from_scene(scene),
                language=_shot_list_language(actor, data.get("language")),
            )
        )


class SceneStoryboardShotListJobView(StoryboardAuthedView):
    throttle_classes = [StoryboardShotListRateThrottle]

    @handle_storyboard_errors
    def post(self, request, project_id: int, scene_id: int):
        actor = self.actor(request)
        data = _validated(ShotListJobCreateSerializer, request.data)
        return Response(shot_list_jobs.enqueue_shot_list(
            actor=actor, project_id=project_id, scene_id=scene_id,
            request_id=data["requestId"], model=data.get("model"),
            max_shots=data["maxShots"], estimated_seconds=data["estimatedSeconds"],
            language=_shot_list_language(actor, data.get("language")),
        ), status=status.HTTP_202_ACCEPTED)


class StoryboardShotListJobListView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int):
        return Response(shot_list_jobs.list_shot_list_jobs(
            actor=self.actor(request), project_id=project_id,
        ))


class StoryboardShotListJobDetailView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int, job_id):
        return Response(shot_list_jobs.get_shot_list_job(
            actor=self.actor(request), project_id=project_id, job_id=job_id,
        ))


class StoryboardShotListJobApplyView(StoryboardAuthedView):
    @handle_storyboard_errors
    def post(self, request, project_id: int, job_id):
        actor = self.actor(request)
        data = _validated(ShotListJobApplySerializer, request.data)
        return Response(shot_list_jobs.apply_shot_list_job(
            actor=actor, project_id=project_id, job_id=job_id,
            expected_revision=data["expectedRevision"], mutation_id=data["mutationId"],
        ))


class StoryboardShotListJobDismissView(StoryboardAuthedView):
    @handle_storyboard_errors
    def post(self, request, project_id: int, job_id):
        return Response(shot_list_jobs.dismiss_shot_list_job(
            actor=self.actor(request), project_id=project_id, job_id=job_id,
        ))


class SceneStoryboardPreviewView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int, scene_id: int):
        return Response(
            services.storyboard_preview(
                actor=self.actor(request),
                project_id=project_id,
                scene_id=scene_id,
                request=request,
            )
        )


class StoryboardShotCollectionView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int, storyboard_id: int):
        return Response(
            services.list_shots(
                actor=self.actor(request),
                project_id=project_id,
                storyboard_id=storyboard_id,
                request=request,
            )
        )

    @handle_storyboard_errors
    def post(self, request, project_id: int, storyboard_id: int):
        data = _validated(ShotCreateSerializer, request.data)
        return Response(
            services.create_shot(
                actor=self.actor(request),
                project_id=project_id,
                storyboard_id=storyboard_id,
                data=data,
                request=request,
            ),
            status=status.HTTP_201_CREATED,
        )


class StoryboardShotReorderView(StoryboardAuthedView):
    @handle_storyboard_errors
    def post(self, request, project_id: int, storyboard_id: int):
        data = _validated(ShotReorderSerializer, request.data)
        return Response(
            {
                "shots": services.reorder_shots(
                    actor=self.actor(request),
                    project_id=project_id,
                    storyboard_id=storyboard_id,
                    shot_ids=data["shotIds"],
                    request=request,
                )
            }
        )


class StoryboardShotDetailView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int, shot_id):
        return Response(
            services.get_shot(
                actor=self.actor(request),
                project_id=project_id,
                shot_id=shot_id,
                request=request,
            )
        )

    @handle_storyboard_errors
    def patch(self, request, project_id: int, shot_id):
        data = _validated(ShotPatchSerializer, request.data)
        return Response(
            services.update_shot(
                actor=self.actor(request),
                project_id=project_id,
                shot_id=shot_id,
                data=data,
                request=request,
            )
        )

    @handle_storyboard_errors
    def delete(self, request, project_id: int, shot_id):
        services.delete_shot(
            actor=self.actor(request),
            project_id=project_id,
            shot_id=shot_id,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class StoryboardShotDuplicateView(StoryboardAuthedView):
    @handle_storyboard_errors
    def post(self, request, project_id: int, shot_id):
        return Response(
            services.duplicate_shot(
                actor=self.actor(request),
                project_id=project_id,
                shot_id=shot_id,
                request=request,
            ),
            status=status.HTTP_201_CREATED,
        )


class StoryboardKeyframeCollectionView(StoryboardAuthedView):
    @handle_storyboard_errors
    def post(self, request, project_id: int, shot_id):
        data = _validated(KeyframeCreateSerializer, request.data)
        return Response(
            services.add_keyframe(
                actor=self.actor(request),
                project_id=project_id,
                shot_id=shot_id,
                position=data["position"],
                request=request,
            ),
            status=status.HTTP_201_CREATED,
        )


class StoryboardKeyframeDetailView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int, keyframe_id):
        return Response(
            services.get_keyframe(
                actor=self.actor(request),
                project_id=project_id,
                keyframe_id=keyframe_id,
                request=request,
            )
        )

    @handle_storyboard_errors
    def patch(self, request, project_id: int, keyframe_id):
        data = _validated(KeyframePatchSerializer, request.data)
        return Response(
            services.update_keyframe(
                actor=self.actor(request),
                project_id=project_id,
                keyframe_id=keyframe_id,
                position=data["position"],
                request=request,
            )
        )

    @handle_storyboard_errors
    def delete(self, request, project_id: int, keyframe_id):
        services.delete_keyframe(
            actor=self.actor(request),
            project_id=project_id,
            keyframe_id=keyframe_id,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class StoryboardCameraIntentView(StoryboardAuthedView):
    @handle_storyboard_errors
    def put(self, request, project_id: int, keyframe_id):
        data = _validated(CameraIntentSerializer, request.data)
        return Response(
            services.update_camera_intent(
                actor=self.actor(request),
                project_id=project_id,
                keyframe_id=keyframe_id,
                data=data,
            )
        )

    patch = put


class StoryboardSuggestedReferencesView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int, keyframe_id):
        return Response(
            services.suggested_references(
                actor=self.actor(request),
                project_id=project_id,
                keyframe_id=keyframe_id,
            )
        )


class StoryboardGenerationReferencesView(StoryboardAuthedView):
    @handle_storyboard_errors
    def put(self, request, project_id: int, keyframe_id):
        data = _validated(GenerationReferencesReplaceSerializer, request.data)
        return Response(
            {
                "references": services.replace_generation_references(
                    actor=self.actor(request),
                    project_id=project_id,
                    keyframe_id=keyframe_id,
                    items=data["references"],
                )
            }
        )


class StoryboardGenerateView(StoryboardAuthedView):
    @handle_storyboard_errors
    def post(self, request, project_id: int, keyframe_id):
        data = _validated(GenerateKeyframeSerializer, request.data)
        payload, created = generation.enqueue_generation(
            actor=self.actor(request),
            project_id=project_id,
            keyframe_id=keyframe_id,
            data=data,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            request=request,
        )
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class StoryboardTransitionView(StoryboardAuthedView):
    @handle_storyboard_errors
    def patch(self, request, project_id: int, transition_id):
        data = _validated(TransitionPatchSerializer, request.data)
        return Response(
            services.update_transition(
                actor=self.actor(request),
                project_id=project_id,
                transition_id=transition_id,
                movement_override=data["movementOverride"],
            )
        )


class StoryboardGenerationDetailView(StoryboardAuthedView):
    @handle_storyboard_errors
    def get(self, request, project_id: int, generation_id):
        return Response(
            generation.get_generation(
                actor=self.actor(request),
                project_id=project_id,
                generation_id=generation_id,
                request=request,
            )
        )
