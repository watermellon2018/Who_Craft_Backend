"""Detail endpoints for the high-risk concurrent-edit entities.

Scene (holds the script + camera settings) and Location get GET + PATCH
endpoints with optimistic-locking. The client sends the ``version`` it
started editing from; if the stored version has moved on, we return 409 instead
of silently overwriting another member's changes.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.movie.project import policy, project_mutations
from w_craft_back.movie.project.dashboard_models import (
    AssetType,
    Location,
    ProjectAsset,
    Scene,
    SceneStoryboard,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.script_workspace import (
    scene_payload,
    scenes_queryset,
)
from w_craft_back.movie.project.serializers import (
    SceneReorderSerializer,
    SceneStoryboardConfirmSerializer,
    SceneStoryboardUpdateSerializer,
    SceneWorkspaceUpdateSerializer,
)

logger = logging.getLogger(__name__)


def _storyboard_payload(storyboard: SceneStoryboard) -> dict:
    return {
        "sceneId": storyboard.scene_id,
        "assetId": storyboard.asset_id,
        "sourceSceneVersion": storyboard.source_scene_version,
        "confirmedSceneVersion": storyboard.confirmed_scene_version,
        "acceptedSceneVersion": storyboard.accepted_scene_version,
        "currentSceneVersion": storyboard.scene.version,
        "needsReview": storyboard.needs_review,
        "updatedAt": (
            storyboard.updated_at.isoformat() if storyboard.updated_at else None
        ),
    }


def _resolve_user(request) -> Optional[User]:
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user
    return None


def _conflict(current_version: int):
    return Response(
        {
            "code": "VERSION_CONFLICT",
            "detail": (
                "Этот объект был изменён другим участником после того, как вы "
                "открыли страницу. Обновите данные перед сохранением."
            ),
            "currentVersion": current_version,
        },
        status=status.HTTP_409_CONFLICT,
    )


def _mutation_error_response(exc):
    if isinstance(exc, project_mutations.VersionConflict):
        return _conflict(exc.current_version)
    if isinstance(exc, project_mutations.ProjectMutationForbidden):
        return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)
    if isinstance(exc, ValidationError):
        return Response(
            {
                "detail": "validation error",
                "errors": getattr(exc, "message_dict", {"detail": exc.messages}),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(exc, ObjectDoesNotExist):
        return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    raise exc


class _VersionedEntityView(APIView):
    """Base for GET/PATCH with optimistic-lock semantics.

    Subclasses define ``model``, ``url_kwarg``, ``editable_fields`` and the
    ``serialize`` method.
    """

    model = None
    url_kwarg = "pk"
    editable_fields: tuple = ()

    # -- helpers ------------------------------------------------------------- #

    def _resolve(self, request, project_id, **kwargs):
        user = _resolve_user(request)
        if user is None:
            return None, None, None, Response(
                {"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED
            )
        project = get_object_or_404(
            Project.objects.select_related("owner"), pk=project_id
        )
        if not policy.can_view(user, project):
            return None, None, None, Response(
                {"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN
            )
        obj_id = kwargs[self.url_kwarg]
        obj = self.model.objects.filter(pk=obj_id, project=project).first()
        if obj is None:
            # Re-scoped to the access-checked project — cross-project ids 404.
            return None, None, None, Response(
                {"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND
            )
        return user, project, obj, None

    def serialize(self, obj) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- handlers ------------------------------------------------------------ #

    def get(self, request, project_id, **kwargs):
        user, project, obj, err = self._resolve(request, project_id, **kwargs)
        if err:
            return err
        return Response(self.serialize(obj))

    def patch(self, request, project_id, **kwargs):
        user, project, obj, err = self._resolve(request, project_id, **kwargs)
        if err:
            return err

        data = request.data if isinstance(request.data, dict) else {}
        expected = data.get("version")
        if expected is not None:
            try:
                expected = int(expected)
            except (TypeError, ValueError):
                return Response(
                    {
                        "code": "VALIDATION_ERROR",
                        "detail": "version must be an integer",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        changes = {
            field: data[field]
            for field in self.editable_fields
            if field in data
        }
        try:
            obj = project_mutations.update_versioned_entity(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project.id,
                model=self.model,
                object_id=obj.pk,
                expected_version=expected,
                changes=changes,
            )
        except (
            ObjectDoesNotExist,
            project_mutations.ProjectMutationForbidden,
            project_mutations.VersionConflict,
            ValidationError,
        ) as exc:
            return _mutation_error_response(exc)
        return Response(self.serialize(obj))


class SceneDetailView(_VersionedEntityView):
    model = Scene
    url_kwarg = "scene_id"

    def serialize(self, scene: Scene, request=None) -> dict:
        hydrated = scenes_queryset(scene.project).get(pk=scene.pk)
        return scene_payload(hydrated, request)

    def get(self, request, project_id, **kwargs):
        user, project, obj, err = self._resolve(request, project_id, **kwargs)
        if err:
            return err
        return Response(self.serialize(obj, request))

    def patch(self, request, project_id, **kwargs):
        user, project, obj, err = self._resolve(request, project_id, **kwargs)
        if err:
            return err

        serializer = SceneWorkspaceUpdateSerializer(
            data=request.data,
            context={"project": project},
        )
        if not serializer.is_valid():
            return Response(
                {"detail": "validation error", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = dict(serializer.validated_data)
        expected = data.pop("version")
        character_ids = data.pop("character_ids", None)
        location_supplied = "location_id" in data
        location_id = data.pop("location_id", None)

        try:
            scene = project_mutations.update_scene(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project.id,
                scene_id=obj.pk,
                expected_version=expected,
                data=data,
                character_ids=character_ids,
                location_supplied=location_supplied,
                location_id=location_id,
            )
        except (
            ObjectDoesNotExist,
            project_mutations.ProjectMutationForbidden,
            project_mutations.VersionConflict,
            ValidationError,
        ) as exc:
            return _mutation_error_response(exc)
        return Response(self.serialize(scene, request))

    def delete(self, request, project_id, **kwargs):
        user, project, obj, err = self._resolve(request, project_id, **kwargs)
        if err:
            return err
        try:
            project_mutations.delete_project_entity(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project.id,
                model=Scene,
                object_id=obj.pk,
            )
        except (
            ObjectDoesNotExist,
            project_mutations.ProjectMutationForbidden,
        ) as exc:
            return _mutation_error_response(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SceneReorderView(APIView):
    """Atomically update the complete scene order and act placement."""

    def patch(self, request, project_id):
        user = _resolve_user(request)
        if user is None:
            return Response(
                {"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED
            )

        serializer = SceneReorderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "validation error", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            scenes = project_mutations.reorder_scenes(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project_id,
                placements=serializer.validated_data["scenes"],
            )
        except (
            ObjectDoesNotExist,
            project_mutations.ProjectMutationForbidden,
            project_mutations.VersionConflict,
            ValidationError,
        ) as exc:
            return _mutation_error_response(exc)

        return Response(
            {
                "scenes": [
                    {
                        "id": scene.pk,
                        "order": scene.order,
                        "act": scene.act,
                        "version": scene.version,
                        "updatedAt": (
                            scene.updated_at.isoformat() if scene.updated_at else ""
                        ),
                    }
                    for scene in scenes
                ]
            }
        )


class SceneStoryboardView(APIView):
    def _resolve(self, request, project_id: int, scene_id: int, *, edit=False):
        user = _resolve_user(request)
        if user is None:
            return None, None, Response(
                {"detail": "Unauthorized"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        project = get_object_or_404(Project, pk=project_id)
        allowed = (
            policy.can_edit(user, project)
            if edit
            else policy.can_view(user, project)
        )
        if not allowed:
            return None, None, Response(
                {"detail": "Forbidden"},
                status=status.HTTP_403_FORBIDDEN,
            )
        scene = Scene.objects.filter(pk=scene_id, project=project).first()
        if scene is None:
            return None, None, Response(
                {"detail": "Not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return user, scene, None

    def get(self, request, project_id: int, scene_id: int):
        _user, scene, err = self._resolve(request, project_id, scene_id)
        if err:
            return err
        storyboard = SceneStoryboard.objects.filter(scene=scene).first()
        if storyboard is None:
            return Response(
                {"detail": "Not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        from w_craft_back.movie.storyboard import services as storyboard_services

        structured = storyboard_services.get_scene_storyboard(
            actor=_user,
            project_id=project_id,
            scene_id=scene_id,
            request=request,
        )
        return Response({**_storyboard_payload(storyboard), **structured})

    def post(self, request, project_id: int, scene_id: int):
        user, _scene_obj, err = self._resolve(
            request,
            project_id,
            scene_id,
            edit=True,
        )
        if err:
            return err
        from w_craft_back.movie.storyboard import services as storyboard_services

        payload, created = storyboard_services.initialize_storyboard(
            actor=user,
            project_id=project_id,
            scene_id=scene_id,
            request=request,
        )
        return Response(
            payload,
            status=(
                status.HTTP_201_CREATED if created else status.HTTP_200_OK
            ),
        )

    def put(self, request, project_id: int, scene_id: int):
        user, scene, err = self._resolve(
            request,
            project_id,
            scene_id,
            edit=True,
        )
        if err:
            return err
        serializer = SceneStoryboardUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "validation error", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = serializer.validated_data
        asset = ProjectAsset.objects.filter(
            pk=data["assetId"],
            project_id=project_id,
            asset_type=AssetType.STORYBOARD,
        ).first()
        if asset is None:
            return Response(
                {"detail": "Storyboard asset not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            with transaction.atomic():
                locked_scene = Scene.objects.select_for_update().get(pk=scene.pk)
                if data["sourceSceneVersion"] > locked_scene.version:
                    return Response(
                        {
                            "detail": (
                                "sourceSceneVersion cannot be newer than the scene"
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                storyboard = (
                    SceneStoryboard.objects.select_for_update()
                    .filter(scene=locked_scene)
                    .first()
                )
                created = storyboard is None
                if created:
                    storyboard = SceneStoryboard.objects.create(
                        scene=locked_scene,
                        asset=asset,
                        source_scene_version=data["sourceSceneVersion"],
                        created_by=user,
                        updated_by=user,
                    )
                else:
                    storyboard.asset = asset
                    storyboard.source_scene_version = data["sourceSceneVersion"]
                    storyboard.confirmed_scene_version = None
                    storyboard.updated_by = user
                    storyboard.save()
        except ValidationError as exc:
            return _mutation_error_response(exc)
        return Response(
            _storyboard_payload(storyboard),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SceneStoryboardConfirmView(APIView):
    def post(self, request, project_id: int, scene_id: int):
        user = _resolve_user(request)
        if user is None:
            return Response(
                {"detail": "Unauthorized"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        project = get_object_or_404(Project, pk=project_id)
        if not policy.can_edit(user, project):
            return Response(
                {"detail": "Forbidden"},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = SceneStoryboardConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "validation error", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            scene = (
                Scene.objects.select_for_update()
                .filter(pk=scene_id, project=project)
                .first()
            )
            if scene is None:
                return Response(
                    {"detail": "Not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            expected = serializer.validated_data["expectedSceneVersion"]
            if expected != scene.version:
                return _conflict(scene.version)
            storyboard = (
                SceneStoryboard.objects.select_for_update()
                .filter(scene=scene)
                .first()
            )
            if storyboard is None:
                return Response(
                    {"detail": "Storyboard not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            storyboard.confirmed_scene_version = scene.version
            storyboard.updated_by = user
            storyboard.save(
                update_fields=[
                    "confirmed_scene_version",
                    "updated_by",
                    "updated_at",
                ]
            )
        return Response(_storyboard_payload(storyboard))


class LocationDetailView(_VersionedEntityView):
    model = Location
    url_kwarg = "location_id"
    editable_fields = ("name", "description", "is_created")

    def serialize(self, loc: Location) -> dict:
        return {
            "id": loc.id,
            "name": loc.name,
            "description": loc.description,
            "isCreated": loc.is_created,
            "version": loc.version,
            "updatedAt": loc.updated_at.isoformat() if loc.updated_at else None,
            "updatedById": loc.updated_by_id,
            "updatedByUsername": (
                loc.updated_by.username if loc.updated_by_id else None
            ),
        }
