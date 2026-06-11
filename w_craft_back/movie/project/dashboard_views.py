"""Project dashboard API views.

Auth follows the project's existing pattern: a ``token_user`` query/body/header
value resolves to a ``UserKey`` -> ``User``. We never trust a user_id from
the request body for access control.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid
from typing import Optional

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Prefetch, Q
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
    build_project_edit_payload,
    build_project_summary,
    record_activity,
)
from w_craft_back.movie.properties.models import Audience, Genre

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Auth helper
# --------------------------------------------------------------------------- #

def _resolve_user(request) -> Optional[User]:
    """Resolve calling ``User`` via the shared token extractor (header → body → deprecated query string)."""
    from w_craft_back.auth.utils import extract_user_token
    token = extract_user_token(request)
    if not token:
        return None
    try:
        return UserKey.objects.select_related("user").get(key=token).user
    except (UserKey.DoesNotExist, ValueError, TypeError):
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


_BASE64_PREFIX_RE = re.compile(r"^data:(?P<mime>[\w/+\-.]+);base64,(?P<data>.+)$", re.S)


def _decode_poster_data_url(data_url: str, owner_id, title: str) -> Optional[ContentFile]:
    """Convert a data: URL into a Django ContentFile usable with ImageField.save."""
    if not data_url:
        return None
    m = _BASE64_PREFIX_RE.match(data_url.strip())
    if not m:
        return None
    mime = m.group("mime").lower()
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        return None
    try:
        raw = base64.b64decode(m.group("data"), validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    if len(raw) > 5 * 1024 * 1024:  # 5 MB hard cap
        return None
    ext = mime.split("/")[-1]
    safe_title = re.sub(r"[^\w\-.]+", "_", title or "project")[:60]
    name = f"{owner_id or 'anon'}/{safe_title}/{uuid.uuid4()}.{ext}"
    return ContentFile(raw, name=name)


def _apply_poster(project: Project, data: dict, owner_id) -> None:
    """Apply poster_image_data (base64) or poster_url ('' clears) to the project."""
    if "poster_image_data" in data and data["poster_image_data"]:
        decoded = _decode_poster_data_url(
            data["poster_image_data"], owner_id, project.title
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

        # owner via direct FK, owner via legacy UserKey, or member
        legacy_owner_q = Q(user__user_id=user.id)
        owner_q = Q(owner_id=user.id)
        member_q = Q(members__user_id=user.id)

        # Annotate counts and prefetch tags so build_project_summary doesn't
        # fire 3 extra queries per project (was an N+1 hot spot for the
        # projects list endpoint).
        projects = (
            Project.objects.filter(owner_q | legacy_owner_q | member_q)
            .select_related("progress")
            .prefetch_related(
                Prefetch(
                    "tags",
                    queryset=ProjectTag.objects.order_by("created_at"),
                )
            )
            .annotate(
                _chars_total=Count("studio_characters", distinct=True),
                _scenes_total=Count("scenes", distinct=True),
            )
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
            synopsis = data.get("synopsis", "") or ""
            project = Project.objects.create(
                owner=user,
                title=data["title"],
                description=data.get("description", "") or synopsis,
                status=data.get("status", ProjectStatus.DRAFT),
                is_favorite=data.get("is_favorite", False),
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
        return Response(build_project_edit_payload(project, request))

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

        editor_fields = (
            "format", "genre", "audience", "annotation", "synopsis",
            "poster_image_data", "poster_url",
        )

        with transaction.atomic():
            for field in ("title", "description", "status", "is_favorite"):
                if field in data:
                    setattr(project, field, data[field])
            if "description" in data:
                project.desc = data["description"]
            if "format" in data:
                project.format = data["format"] or ""
            if "annotation" in data:
                project.annot = data["annotation"] or ""
            if "synopsis" in data:
                project.desc = data["synopsis"] or ""

            # Maintain archived_at when transitioning to/from archived.
            if "status" in data:
                if data["status"] == ProjectStatus.ARCHIVED and project.archived_at is None:
                    project.archived_at = _tz.now()
                elif data["status"] != ProjectStatus.ARCHIVED and project.archived_at is not None:
                    project.archived_at = None

            _apply_poster(project, data, owner_id=user.id)
            project.save()

            if "tags" in data:
                _replace_tags(project, data["tags"])
            if "genre" in data:
                project.genre.set(_resolve_genres(data["genre"]))
            if "audience" in data:
                project.audience.set(_resolve_audiences(data["audience"]))

            # Differentiated activity entries.
            new_status = data.get("status")
            status_changed = new_status is not None and new_status != prev_status
            non_status_changed = any(
                f in data
                for f in ("title", "description", "is_favorite", "tags") + editor_fields
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

        return Response(build_project_edit_payload(project, request))

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
