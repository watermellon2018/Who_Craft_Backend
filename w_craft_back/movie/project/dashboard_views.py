"""Project dashboard API views.

Auth follows the project's existing pattern: a ``token_user`` query/body/header
value resolves to a ``UserKey`` -> ``User``. We never trust a user_id from
the request body for access control.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.dashboard_models import (
    Location,
    MusicTrack,
    ProjectActivity,
    ProjectGenerationJob,
    ProjectGenerationJobStatus,
    ProjectMember,
    ProjectMemberRole,
    ProjectProgress,
    ProjectTag,
    Scene,
)
from w_craft_back.movie.project.models import Project, ProjectStatus
from w_craft_back.movie.project.permissions import (
    user_can_edit_project,
    user_has_project_access,
    user_is_project_owner,
)
from w_craft_back.movie.project.serializers import (
    CharacterCreateSerializer,
    GenerationJobCreateSerializer,
    LocationCreateSerializer,
    MusicTrackCreateSerializer,
    ProjectCreateSerializer,
    ProjectUpdateSerializer,
    SceneCreateSerializer,
)
from w_craft_back.movie.project.services import (
    build_project_dashboard,
    build_project_summary,
    record_activity,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Auth helper
# --------------------------------------------------------------------------- #

def _resolve_user(request) -> Optional[User]:
    token = (
        request.query_params.get("token_user")
        or (request.data.get("token_user") if hasattr(request, "data") else None)
        or request.headers.get("X-User-Token")
    )
    if not token:
        return None
    try:
        return UserKey.objects.select_related("user").get(key=token).user
    except (UserKey.DoesNotExist, ValueError, Exception):
        return None


def _unauthorized():
    return Response({"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)


def _forbidden():
    return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)


def _validation_error(errors):
    return Response(
        {"detail": "validation error", "errors": errors},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _get_project_or_404(project_id) -> Project:
    return get_object_or_404(
        Project.objects.select_related("owner", "user", "progress"),
        pk=project_id,
    )


# --------------------------------------------------------------------------- #
# Tags helper
# --------------------------------------------------------------------------- #

def _replace_tags(project: Project, names) -> None:
    cleaned = []
    seen = set()
    for raw in names or []:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        cleaned.append(name)
    ProjectTag.objects.filter(project=project).delete()
    ProjectTag.objects.bulk_create([ProjectTag(project=project, name=n) for n in cleaned])


# --------------------------------------------------------------------------- #
# Project list / create
# --------------------------------------------------------------------------- #

class ProjectListCreateView(APIView):
    def get(self, request):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()

        # owner via direct FK, owner via legacy UserKey, or member
        legacy_owner_q = Q(user__user_id=user.id)
        owner_q = Q(owner_id=user.id)
        member_q = Q(members__user_id=user.id)

        projects = (
            Project.objects.filter(owner_q | legacy_owner_q | member_q)
            .select_related("progress")
            .distinct()
            .order_by("-updated_at", "-created_at", "-id")
        )
        data = [build_project_summary(p, request) for p in projects]
        return Response({"projects": data})

    def post(self, request):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()

        serializer = ProjectCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        with transaction.atomic():
            project = Project.objects.create(
                owner=user,
                title=data["title"],
                description=data.get("description", ""),
                status=data.get("status", ProjectStatus.DRAFT),
                is_favorite=data.get("is_favorite", False),
                # legacy required fields kept blank-safe
                user_id=_legacy_userkey_id(user),
                format="",
                annot="",
                desc=data.get("description", ""),
            )
            _replace_tags(project, data.get("tags", []))
            ProjectMember.objects.get_or_create(
                project=project,
                user=user,
                defaults={"role": ProjectMemberRole.OWNER},
            )
            ProjectProgress.objects.get_or_create(project=project)
            record_activity(
                project,
                user,
                "project_updated",
                title=project.title,
                description="проект создан",
            )

        return Response(build_project_summary(project, request), status=status.HTTP_201_CREATED)


def _legacy_userkey_id(user: User) -> Optional[int]:
    """Return UserKey.id for the user (if any). Legacy Project.user FK is non-null,
    so we make sure each new Project has one to avoid breaking older code paths."""
    uk = UserKey.objects.filter(user=user).first()
    if uk is None:
        uk = UserKey.objects.create(user=user)
    return uk.id


# --------------------------------------------------------------------------- #
# Project detail / update / delete
# --------------------------------------------------------------------------- #

class ProjectDetailView(APIView):
    def get(self, request, project_id: int):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()
        project = _get_project_or_404(project_id)
        if not user_has_project_access(user, project):
            return _forbidden()
        return Response(build_project_summary(project, request))

    def patch(self, request, project_id: int):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()
        project = _get_project_or_404(project_id)
        if not user_can_edit_project(user, project):
            return _forbidden()

        serializer = ProjectUpdateSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        from django.utils import timezone as _tz

        prev_status = project.status

        with transaction.atomic():
            for field in ("title", "description", "status", "is_favorite"):
                if field in data:
                    setattr(project, field, data[field])
            if "description" in data:
                project.desc = data["description"]
            # Maintain archived_at when transitioning to/from archived.
            if "status" in data:
                if data["status"] == ProjectStatus.ARCHIVED and project.archived_at is None:
                    project.archived_at = _tz.now()
                elif data["status"] != ProjectStatus.ARCHIVED and project.archived_at is not None:
                    project.archived_at = None
            project.save()
            if "tags" in data:
                _replace_tags(project, data["tags"])

            # Differentiated activity entries.
            new_status = data.get("status")
            status_changed = new_status is not None and new_status != prev_status
            non_status_changed = any(
                f in data for f in ("title", "description", "is_favorite", "tags")
            )

            if status_changed and new_status == ProjectStatus.ARCHIVED:
                record_activity(
                    project,
                    user,
                    "project_archived",
                    title=project.title,
                    description="проект архивирован",
                    metadata={"from": prev_status, "to": new_status},
                )
            elif status_changed:
                record_activity(
                    project,
                    user,
                    "project_status_changed",
                    title=project.title,
                    description="статус проекта изменён",
                    metadata={"from": prev_status, "to": new_status},
                )

            if non_status_changed:
                record_activity(
                    project,
                    user,
                    "project_updated",
                    title=project.title,
                    description="проект обновлён",
                )

        return Response(build_project_summary(project, request))

    def delete(self, request, project_id: int):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()
        project = _get_project_or_404(project_id)
        if not user_is_project_owner(user, project):
            return _forbidden()
        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Dashboard endpoint
# --------------------------------------------------------------------------- #

class ProjectDashboardView(APIView):
    def get(self, request, project_id: int):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()
        project = _get_project_or_404(project_id)
        if not user_has_project_access(user, project):
            return _forbidden()
        payload = build_project_dashboard(project, user, request)
        return Response(payload)


# --------------------------------------------------------------------------- #
# Action endpoints
# --------------------------------------------------------------------------- #

class _ProjectScopedView(APIView):
    """Base for endpoints that need an editable project."""

    def _editable_project(self, request, project_id: int):
        user = _resolve_user(request)
        if user is None:
            return None, None, _unauthorized()
        project = _get_project_or_404(project_id)
        if not user_can_edit_project(user, project):
            return None, None, _forbidden()
        return user, project, None


class ProjectCharactersView(_ProjectScopedView):
    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        serializer = CharacterCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        # Lazy import — StudioCharacter is the canonical character entity.
        from w_craft_back.character_studio.models import StudioCharacter

        legacy_userkey_id = _legacy_userkey_id(user)
        with transaction.atomic():
            character = StudioCharacter.objects.create(
                project=project,
                user_id=legacy_userkey_id,
                name=data["name"],
                short_description=data.get("short_description", ""),
                role=data.get("role", "secondary"),
            )
            record_activity(
                project,
                user,
                "character_created",
                title=character.name,
                description="персонаж создан",
                metadata={"character_id": str(character.character_id)},
            )

        return Response(
            {
                "id": str(character.character_id),
                "name": character.name,
                "role": character.role,
                "shortDescription": character.short_description,
            },
            status=status.HTTP_201_CREATED,
        )


class ProjectScenesView(_ProjectScopedView):
    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        serializer = SceneCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        location = None
        if data.get("location_id"):
            location = Location.objects.filter(
                pk=data["location_id"], project=project
            ).first()
            if location is None:
                return _validation_error(
                    {"location_id": ["location not found in this project"]}
                )

        order = data.get("order")
        if order is None:
            order = (Scene.objects.filter(project=project).count() or 0) + 1

        with transaction.atomic():
            scene = Scene.objects.create(
                project=project,
                title=data["title"],
                description=data.get("description", ""),
                script_text=data.get("script_text", ""),
                location=location,
                order=order,
            )
            record_activity(
                project,
                user,
                "scene_created",
                title=scene.title,
                description="сцена создана",
                metadata={"scene_id": scene.id},
            )

        return Response(
            {"id": scene.id, "title": scene.title, "order": scene.order},
            status=status.HTTP_201_CREATED,
        )


class ProjectMusicView(_ProjectScopedView):
    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        serializer = MusicTrackCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        with transaction.atomic():
            track = MusicTrack.objects.create(
                project=project,
                title=data["title"],
                author=data.get("author", ""),
                duration_seconds=data.get("duration_seconds", 0),
                tags=data.get("tags", []),
            )
            record_activity(
                project,
                user,
                "music_added",
                title=track.title,
                description=track.author or "",
                metadata={"track_id": track.id},
            )

        return Response(
            {"id": track.id, "title": track.title}, status=status.HTTP_201_CREATED
        )


class ProjectLocationsView(_ProjectScopedView):
    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        serializer = LocationCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        with transaction.atomic():
            location = Location.objects.create(
                project=project,
                name=data["name"],
                description=data.get("description", ""),
            )
            record_activity(
                project,
                user,
                "location_created",
                title=location.name,
                description="локация создана",
                metadata={"location_id": location.id},
            )

        return Response(
            {"id": location.id, "name": location.name}, status=status.HTTP_201_CREATED
        )


class ProjectAssetsView(_ProjectScopedView):
    parser_classes = []  # default parsers; multipart works out of the box

    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        from w_craft_back.movie.project.dashboard_models import AssetType, ProjectAsset

        upload = request.FILES.get("file")
        if upload is None:
            return _validation_error({"file": ["this field is required"]})
        asset_type = request.data.get("asset_type") or "reference"
        if asset_type not in {c[0] for c in AssetType.choices}:
            return _validation_error({"asset_type": ["invalid"]})
        title = request.data.get("title", "") or ""

        with transaction.atomic():
            asset = ProjectAsset.objects.create(
                project=project,
                uploaded_by=user,
                file=upload,
                asset_type=asset_type,
                title=title,
            )
            record_activity(
                project,
                user,
                "asset_uploaded",
                title=title or upload.name,
                description=asset_type,
                metadata={"asset_id": asset.id, "asset_type": asset_type},
            )

        return Response(
            {"id": asset.id, "asset_type": asset.asset_type},
            status=status.HTTP_201_CREATED,
        )


class ProjectGenerationJobsView(_ProjectScopedView):
    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        serializer = GenerationJobCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        from w_craft_back.movie.project.dashboard_models import GenerationJobType

        if data["job_type"] not in {c[0] for c in GenerationJobType.choices}:
            return _validation_error({"job_type": ["invalid"]})

        job = ProjectGenerationJob.objects.create(
            project=project,
            user=user,
            job_type=data["job_type"],
            status=ProjectGenerationJobStatus.QUEUED,
            prompt=data.get("prompt", ""),
            negative_prompt=data.get("negative_prompt", ""),
            input_data=data.get("input_data", {}),
        )
        return Response(
            {"id": job.id, "status": job.status, "jobType": job.job_type},
            status=status.HTTP_201_CREATED,
        )
