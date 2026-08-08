"""Project dashboard API views.

DRF resolves the ``X-User-Token`` access token before these handlers run. The
temporary body-token fallback is implemented centrally; query credentials are
never accepted. We never trust a request-body user id for access control.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.db.models.deletion import ProtectedError, RestrictedError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project import (
    policy,
    project_mutations,
    team_errors,
    team_service,
)
from w_craft_back.movie.project.dashboard_models import (
    ProjectAsset,
    ProjectMember,
    ProjectMemberRole,
    ProjectProgress,
    ProjectTag,
)
from w_craft_back.movie.project.models import Project, ProjectStatus
from w_craft_back.movie.project.project_images import (
    decode_project_image_data_url,
)
from w_craft_back.movie.project.permissions import (
    user_has_project_access,
)
from w_craft_back.movie.project.serializers import (
    CharacterCreateSerializer,
    LocationCreateSerializer,
    MusicTrackCreateSerializer,
    ProjectCreateSerializer,
    ProjectUpdateSerializer,
)
from w_craft_back.movie.project.serializers import SceneWorkspaceCreateSerializer
from w_craft_back.movie.project.services import (
    build_project_dashboard,
    build_project_edit_payload,
    build_project_summary,
    record_activity,
)
from w_craft_back.movie.project.script_workspace import (
    characters_collection_payload,
    scene_payload,
)
from w_craft_back.storage_gateway import signed_url_for_file

from w_craft_back.movie.properties.models import Audience, Genre

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Auth helper
# --------------------------------------------------------------------------- #

def _resolve_user(request) -> Optional[User]:
    """Return the Django user established by DRF authentication."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user
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


def _mutation_error_response(exc):
    if isinstance(exc, Project.DoesNotExist):
        return Response(status=status.HTTP_404_NOT_FOUND)
    if isinstance(exc, project_mutations.ProjectMutationForbidden):
        return _forbidden()
    if isinstance(exc, ValidationError):
        return _validation_error(
            getattr(exc, "message_dict", {"detail": exc.messages})
        )
    raise exc


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
    ProjectTag.objects.bulk_create(
        [ProjectTag(project=project, name=name) for name in cleaned]
    )


# --------------------------------------------------------------------------- #
# Editor helpers (genre / audience / poster)
# --------------------------------------------------------------------------- #

def _slugify_genre(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^a-z0-9_\-]", "", value)
    return value or "genre"


# Mirror of frontend src/constants/projectOptions.ts — used so that when we
# auto-create a Genre row for a known editor value, we store the readable
# Russian label in `name` rather than the raw machine value.
_GENRE_LABELS = {
    "drama": "Драма",
    "comedy": "Комедия",
    "action": "Экшен",
    "thriller": "Триллер",
    "horror": "Хоррор",
    "sci_fi": "Научная фантастика",
    "fantasy": "Фэнтези",
    "adventure": "Приключения",
    "romance": "Романтика",
    "detective": "Детектив",
    "mystery": "Мистика",
    "crime": "Криминал",
    "historical": "Исторический",
    "documentary": "Документальный",
    "animation": "Анимация",
    "family": "Семейный",
    "musical": "Мюзикл",
    "war": "Военный",
    "western": "Вестерн",
    "cyberpunk": "Киберпанк",
    "post_apocalyptic": "Постапокалипсис",
    "slice_of_life": "Повседневность",
    "superhero": "Супергерои",
    "other": "Другое",
}


def _resolve_genres(values) -> list[Genre]:
    """Return Genre rows for the supplied translit/name values, creating
    user-defined entries on the fly (frontend allows free-form genres)."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        v = raw.strip()
        if not v or v.lower() in seen:
            continue
        seen.add(v.lower())
        cleaned.append(v)

    if not cleaned:
        return []

    existing = list(
        Genre.objects.filter(Q(translit__in=cleaned) | Q(name__in=cleaned))
    )
    by_translit = {g.translit: g for g in existing}
    by_name = {g.name: g for g in existing}

    resolved: list[Genre] = []
    seen_ids: set[int] = set()
    for value in cleaned:
        match = by_translit.get(value) or by_name.get(value)
        if match is None:
            translit = _slugify_genre(value)
            base_translit = translit
            suffix = 1
            while Genre.objects.filter(translit=translit).exists():
                suffix += 1
                translit = f"{base_translit}_{suffix}"
            display_name = _GENRE_LABELS.get(value.lower(), value)
            match = Genre.objects.create(name=display_name, translit=translit)
        if match.id in seen_ids:
            continue
        seen_ids.add(match.id)
        resolved.append(match)
    return resolved


# Mirror of frontend src/constants/projectOptions.ts. Keys are stable English
# values stored as Audience.translit; the dict gives us the readable Russian
# name to use when we have to auto-create the row.
_AUDIENCE_LABELS = {
    "all": "Все",
    "kids": "Дети",
    "teens": "Подростки",
    "young_adults": "Молодёжь",
    "adults": "Взрослые",
    "elderly": "Пожилые люди",
}

# Allowed values accepted on write. Anything outside this set gets dropped.
_AUDIENCE_ALLOWED = set(_AUDIENCE_LABELS.keys())


def _normalize_audience_values(values) -> list[str]:
    """Normalize the raw values from the editor:

    - drop anything outside the canonical set,
    - dedupe,
    - if "all" is present alongside specific values, collapse to ["all"],
    - if the result is empty, fall back to ["all"] so the project always has
      at least one audience.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        v = raw.strip().lower()
        if v not in _AUDIENCE_ALLOWED or v in seen:
            continue
        seen.add(v)
        cleaned.append(v)

    if not cleaned:
        return ["all"]
    if "all" in cleaned:
        return ["all"]
    return cleaned


def _resolve_audiences(values) -> list[Audience]:
    """Return Audience rows for the editor values, creating canonical rows
    on the fly so the legacy M2M storage stays in sync with the frontend
    value vocabulary."""
    normalized = _normalize_audience_values(values)
    if not normalized:
        return []

    existing = list(Audience.objects.filter(translit__in=normalized))
    by_translit = {a.translit: a for a in existing}

    resolved: list[Audience] = []
    seen_ids: set[int] = set()
    for value in normalized:
        match = by_translit.get(value)
        if match is None:
            display_name = _AUDIENCE_LABELS.get(value, value)
            # Re-fetch race-safe via get_or_create on the unique translit field.
            match, _ = Audience.objects.get_or_create(
                translit=value,
                defaults={"name": display_name},
            )
            by_translit[value] = match
        if match.id in seen_ids:
            continue
        seen_ids.add(match.id)
        resolved.append(match)
    return resolved


def _apply_poster(project: Project, data: dict, owner_id) -> None:
    """Apply poster_image_data (base64) or poster_url ('' clears) to the project."""
    if "poster_image_data" in data and data["poster_image_data"]:
        decoded = decode_project_image_data_url(
            data["poster_image_data"],
            owner_id=owner_id,
            title=project.title,
        )
        if decoded is not None:
            old = project.image
            if old:
                try:
                    old.delete(save=False)
                except Exception:  # pragma: no cover - filesystem race
                    logger.warning("Failed to delete old poster", exc_info=True)
            project.image.save(decoded.name, decoded, save=False)
        return

    if "poster_url" in data and data["poster_url"] in (None, ""):
        if project.image:
            try:
                project.image.delete(save=False)
            except Exception:  # pragma: no cover
                logger.warning("Failed to delete poster on clear", exc_info=True)
        project.image = ""


# --------------------------------------------------------------------------- #
# Project list / create
# --------------------------------------------------------------------------- #

class ProjectListCreateView(APIView):
    def get(self, request):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()

        # Canonical owner or project member. Legacy creator attribution is not
        # an access-control signal.
        owner_q = Q(owner_id=user.id)
        member_q = Q(members__user_id=user.id)

        # Annotate counts and prefetch tags so build_project_summary doesn't
        # fire 3 extra queries per project (was an N+1 hot spot for the
        # projects list endpoint).
        projects = (
            Project.objects.filter(owner_q | member_q)
            .select_related("progress", "owner", "user", "poster__selected_variant")
            .prefetch_related(
                Prefetch(
                    "tags",
                    queryset=ProjectTag.objects.order_by("created_at"),
                ),
                Prefetch(
                    "members",
                    queryset=ProjectMember.objects.select_related("user").order_by(
                        "created_at"
                    ),
                ),
            )
            .annotate(
                _chars_total=Count("studio_characters", distinct=True),
                _scenes_total=Count("scenes", distinct=True),
            )
            .distinct()
            .order_by("-updated_at", "-created_at", "-id")
        )
        data = [build_project_summary(p, request, user=user) for p in projects]
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
            synopsis = data.get("synopsis", "") or ""
            project = Project.objects.create(
                owner=user,
                title=data["title"],
                description=data.get("description", "") or synopsis,
                status=data.get("status", ProjectStatus.DRAFT),
                is_favorite=data.get("is_favorite", False),
                generation_settings=data.get("generation_settings", {}),
                # legacy required fields
                user_id=_legacy_userkey_id(user),
                format=data.get("format", "") or "",
                annot=data.get("annotation", "") or "",
                desc=synopsis or data.get("description", "") or "",
            )
            _replace_tags(project, data.get("tags", []))
            if "genre" in data:
                project.genre.set(_resolve_genres(data["genre"]))
            if "audience" in data:
                project.audience.set(_resolve_audiences(data["audience"]))
            _apply_poster(project, data, owner_id=user.id)
            project.save()

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

        return Response(
            build_project_edit_payload(project, request),
            status=status.HTTP_201_CREATED,
        )


def _legacy_userkey_id(user: User) -> Optional[int]:
    """Return/create legacy creator attribution for compatibility."""
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
        return Response(build_project_edit_payload(project, request))

    def patch(self, request, project_id: int):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()

        try:
            project_mutations.get_project_for_action(
                actor=user,
                project_id=project_id,
                action=policy.Action.EDIT_SETTINGS,
            )
        except (
            Project.DoesNotExist,
            project_mutations.ProjectMutationForbidden,
        ) as exc:
            return _mutation_error_response(exc)

        serializer = ProjectUpdateSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        genres = _resolve_genres(data["genre"]) if "genre" in data else None
        audiences = (
            _resolve_audiences(data["audience"]) if "audience" in data else None
        )
        poster_file = None
        poster_supplied = False
        if data.get("poster_image_data"):
            poster_file = decode_project_image_data_url(
                data["poster_image_data"],
                owner_id=user.id,
                title=data.get("title", "project"),
            )
            if poster_file is None:
                return _validation_error(
                    {"poster_image_data": ["invalid or exceeds 5 MB"]}
                )
            poster_supplied = True
        elif "poster_url" in data and data["poster_url"] in (None, ""):
            poster_supplied = True

        try:
            project = project_mutations.update_project_settings(
                actor=user,
                action=policy.Action.EDIT_SETTINGS,
                project_id=project_id,
                data=data,
                genres=genres,
                audiences=audiences,
                poster_file=poster_file,
                poster_supplied=poster_supplied,
            )
        except (
            Project.DoesNotExist,
            project_mutations.ProjectMutationForbidden,
            ValidationError,
        ) as exc:
            return _mutation_error_response(exc)
        return Response(build_project_edit_payload(project, request))

    def delete(self, request, project_id: int):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()
        project = _get_project_or_404(project_id)
        try:
            team_service.delete_project(user, project.pk)
        except Project.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except team_errors.InsufficientPermissions:
            return _forbidden()
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

    def _project_for_action(
        self,
        request,
        project_id: int,
        action: policy.Action,
    ):
        user = _resolve_user(request)
        if user is None:
            return None, None, _unauthorized()
        try:
            project = project_mutations.get_project_for_action(
                actor=user,
                project_id=project_id,
                action=action,
            )
        except (
            Project.DoesNotExist,
            project_mutations.ProjectMutationForbidden,
        ) as exc:
            return None, None, _mutation_error_response(exc)
        return user, project, None

    def _viewable_project(self, request, project_id: int):
        return self._project_for_action(request, project_id, policy.Action.VIEW)

    def _editable_project(self, request, project_id: int):
        return self._project_for_action(
            request,
            project_id,
            policy.Action.EDIT_CONTENT,
        )


class ProjectCharactersView(_ProjectScopedView):
    def get(self, request, project_id: int):
        user, project, err = self._viewable_project(request, project_id)
        if err:
            return err
        return Response(characters_collection_payload(project, request))

    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        serializer = CharacterCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        try:
            character = project_mutations.create_project_character(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project.id,
                data=data,
            )
        except (
            Project.DoesNotExist,
            project_mutations.ProjectMutationForbidden,
            ValidationError,
        ) as exc:
            return _mutation_error_response(exc)

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
    def get(self, request, project_id: int):
        user, project, err = self._viewable_project(request, project_id)
        if err:
            return err
        from w_craft_back.movie.project.script_workspace import (
            scenes_collection_payload,
        )

        return Response(scenes_collection_payload(project, user, request))

    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        serializer = SceneWorkspaceCreateSerializer(
            data=request.data, context={"project": project}
        )
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        try:
            scene = project_mutations.create_scene(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project.id,
                data=data,
            )
        except (
            Project.DoesNotExist,
            project_mutations.ProjectMutationForbidden,
            ValidationError,
        ) as exc:
            return _mutation_error_response(exc)

        return Response(scene_payload(scene, request), status=status.HTTP_201_CREATED)


class ProjectMusicView(_ProjectScopedView):
    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        serializer = MusicTrackCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer.errors)
        data = serializer.validated_data

        try:
            track = project_mutations.create_music_track(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project.id,
                data=data,
            )
        except (
            Project.DoesNotExist,
            project_mutations.ProjectMutationForbidden,
            ValidationError,
        ) as exc:
            return _mutation_error_response(exc)

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

        try:
            location = project_mutations.create_location(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project.id,
                data=data,
            )
        except (
            Project.DoesNotExist,
            project_mutations.ProjectMutationForbidden,
            ValidationError,
        ) as exc:
            return _mutation_error_response(exc)

        return Response(
            {"id": location.id, "name": location.name}, status=status.HTTP_201_CREATED
        )


class ProjectAssetsView(_ProjectScopedView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, project_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err

        from w_craft_back.movie.project.dashboard_models import AssetType

        upload = request.FILES.get("file")
        if upload is None:
            return _validation_error({"file": ["this field is required"]})
        asset_type = request.data.get("asset_type") or "reference"
        if asset_type not in {c[0] for c in AssetType.choices}:
            return _validation_error({"asset_type": ["invalid"]})
        title = request.data.get("title", "") or ""

        try:
            asset = project_mutations.create_project_asset(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project.id,
                upload=upload,
                asset_type=asset_type,
                title=title,
            )
        except (
            Project.DoesNotExist,
            project_mutations.ProjectMutationForbidden,
            ValidationError,
        ) as exc:
            return _mutation_error_response(exc)

        return Response(
            {
                "id": asset.id,
                "asset_type": asset.asset_type,
                "mime_type": asset.metadata.get("mime_type"),
                "size_bytes": asset.metadata.get("size_bytes"),
                "url": signed_url_for_file(
                    asset.file,
                    request,
                    project=project,
                ),
            },
            status=status.HTTP_201_CREATED,
        )


class ProjectAssetDetailView(_ProjectScopedView):
    """Issue signed downloads and delete assets through project policy."""

    def get(self, request, project_id: int, asset_id: int):
        _user, project, err = self._viewable_project(request, project_id)
        if err:
            return err
        asset = ProjectAsset.objects.filter(
            pk=asset_id,
            project=project,
        ).first()
        if asset is None:
            return Response(
                {"detail": "Not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "id": asset.id,
                "asset_type": asset.asset_type,
                "title": asset.title,
                "mime_type": asset.metadata.get("mime_type"),
                "size_bytes": asset.metadata.get("size_bytes"),
                "url": signed_url_for_file(
                    asset.file,
                    request,
                    project=project,
                ),
            }
        )

    def delete(self, request, project_id: int, asset_id: int):
        user, project, err = self._editable_project(request, project_id)
        if err:
            return err
        try:
            project_mutations.delete_project_entity(
                actor=user,
                action=policy.Action.EDIT_CONTENT,
                project_id=project.id,
                model=ProjectAsset,
                object_id=asset_id,
            )
        except (
            ProjectAsset.DoesNotExist,
            project_mutations.ProjectMutationForbidden,
        ) as exc:
            return _mutation_error_response(exc)
        except (ProtectedError, RestrictedError):
            return Response(
                {
                    "code": "REFERENCE_ASSET_IN_USE",
                    "detail": "Asset is used by a reference version or variant.",
                    "retryable": False,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
