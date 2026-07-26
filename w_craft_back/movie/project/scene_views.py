"""Detail endpoints for the high-risk concurrent-edit entities.

Scene (holds the script + camera settings), Location and MusicTrack get GET +
PATCH endpoints with optimistic-locking. The client sends the ``version`` it
started editing from; if the stored version has moved on, we return 409 instead
of silently overwriting another member's changes.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.movie.project import policy
from w_craft_back.movie.project.dashboard_models import (
    Location,
    MusicTrack,
    Scene,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.script_workspace import (
    replace_scene_characters,
    scene_payload,
    scenes_queryset,
    script_text_from_blocks,
)
from w_craft_back.movie.project.serializers import SceneWorkspaceUpdateSerializer

logger = logging.getLogger(__name__)


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
            Project.objects.select_related("owner", "user"), pk=project_id
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
        if not policy.can_edit(user, project):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        data = request.data if isinstance(request.data, dict) else {}

        # Optimistic-lock check. The client MUST send the version it loaded.
        expected = data.get("version")
        if expected is not None:
            try:
                expected = int(expected)
            except (TypeError, ValueError):
                return Response(
                    {"code": "VALIDATION_ERROR", "detail": "version must be an integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if expected != obj.version:
                return _conflict(obj.version)

        with transaction.atomic():
            # Re-read under lock and re-check version to close the race window.
            locked = self.model.objects.select_for_update().get(pk=obj.pk)
            if expected is not None and expected != locked.version:
                return _conflict(locked.version)

            changed = False
            for field in self.editable_fields:
                if field in data:
                    setattr(locked, field, data[field])
                    changed = True
            if changed:
                locked.version = (locked.version or 1) + 1
                if hasattr(locked, "updated_by_id"):
                    locked.updated_by = user
                locked.save()
            obj = locked
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
        if not policy.can_edit(user, project):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

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
        if expected != obj.version:
            return _conflict(obj.version)

        character_ids = data.pop("character_ids", None)
        location_supplied = "location_id" in data
        location_id = data.pop("location_id", None)
        location = None
        if location_supplied and location_id is not None:
            location = Location.objects.filter(
                pk=location_id,
                project=project,
            ).first()
            if location is None:
                return Response(
                    {
                        "detail": "validation error",
                        "errors": {
                            "location_id": ["location not found in this project"]
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if "script_blocks" in data:
            data["script_text"] = script_text_from_blocks(data["script_blocks"])
        elif "script_text" in data:
            data["script_blocks"] = []

        with transaction.atomic():
            locked = Scene.objects.select_for_update().get(
                pk=obj.pk,
                project=project,
            )
            if expected != locked.version:
                return _conflict(locked.version)

            changed = bool(data) or character_ids is not None or location_supplied
            for field, value in data.items():
                setattr(locked, field, value)
            if location_supplied:
                locked.location = location
            if character_ids is not None:
                replace_scene_characters(locked, project, character_ids)
            if changed:
                locked.version = (locked.version or 1) + 1
                locked.updated_by = user
                locked.save()

        return Response(self.serialize(locked, request))

    def delete(self, request, project_id, **kwargs):
        user, project, obj, err = self._resolve(request, project_id, **kwargs)
        if err:
            return err
        if not policy.can_edit(user, project):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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


class MusicTrackDetailView(_VersionedEntityView):
    model = MusicTrack
    url_kwarg = "track_id"
    editable_fields = ("title", "author", "duration_seconds", "tags")

    def serialize(self, track: MusicTrack) -> dict:
        return {
            "id": track.id,
            "title": track.title,
            "author": track.author,
            "durationSeconds": track.duration_seconds,
            "tags": list(track.tags or []),
            "version": track.version,
            "updatedAt": track.updated_at.isoformat() if track.updated_at else None,
            "updatedById": track.updated_by_id,
            "updatedByUsername": (
                track.updated_by.username if track.updated_by_id else None
            ),
        }
