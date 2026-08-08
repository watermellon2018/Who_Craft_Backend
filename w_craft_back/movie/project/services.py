"""Project dashboard service.

Aggregates everything the project dashboard page needs into a single payload.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Optional

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Q
from django.utils import timezone

from w_craft_back.character_studio.models import CharacterRole, StudioCharacter
from w_craft_back.movie.project.dashboard_models import (
    Location,
    MusicTrack,
    ProjectActivity,
    ProjectAsset,
    ProjectMember,
    ProjectMemberRole,
    ProjectProgress,
    ProjectTag,
    Scene,
    SceneCharacter,
    SceneMusic,
)
from w_craft_back.movie.project.models import Project, ProjectStatus
from w_craft_back.storage_gateway import (
    signed_url_for_asset,
    signed_url_for_file,
    signed_url_for_music_asset,
)

try:  # UserProfile is optional — guard against absence.
    from w_craft_back.profile.models import UserProfile  # type: ignore
except Exception:  # pragma: no cover - defensive
    UserProfile = None  # type: ignore


# --------------------------------------------------------------------------- #
# Label / formatting helpers
# --------------------------------------------------------------------------- #

_STATUS_LABELS = {
    ProjectStatus.DRAFT: "Черновик",
    ProjectStatus.IN_PROGRESS: "В работе",
    ProjectStatus.COMPLETED: "Завершён",
    ProjectStatus.ARCHIVED: "В архиве",
}

_CHARACTER_ROLE_LABELS = {
    CharacterRole.MAIN: "Главная роль",
    CharacterRole.SECONDARY: "Второстепенная",
    CharacterRole.ANTAGONIST: "Антагонист",
    CharacterRole.EPISODIC: "Эпизодический",
    CharacterRole.CAMEO: "Камео",
}


def _clamp_progress(value: Optional[int]) -> int:
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def _format_duration(seconds: Optional[int]) -> str:
    s = int(seconds or 0)
    if s < 0:
        s = 0
    minutes, secs = divmod(s, 60)
    return f"{minutes:02d}:{secs:02d}"


def _format_relative_ru(when: Optional[datetime], prefix: str = "") -> str:
    if when is None:
        return ""
    if timezone.is_naive(when):
        when = when.replace(tzinfo=dt_timezone.utc)
    now = timezone.now()
    delta = now - when
    seconds = int(delta.total_seconds())

    if seconds < 60:
        body = "только что"
        return f"{prefix}{body}".strip() if prefix else body

    if seconds < 3600:
        minutes = max(1, seconds // 60)
        body = f"{minutes} {_plural_ru(minutes, 'минуту', 'минуты', 'минут')} назад"
    elif seconds < 86400:
        hours = seconds // 3600
        body = f"{hours} {_plural_ru(hours, 'час', 'часа', 'часов')} назад"
    elif seconds < 86400 * 7:
        days = seconds // 86400
        body = f"{days} {_plural_ru(days, 'день', 'дня', 'дней')} назад"
    else:
        body = when.strftime("%d.%m.%Y")
        return f"{prefix}{body}".strip() if prefix else body

    return f"{prefix}{body}".strip() if prefix else body


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    n1 = n % 10
    if 10 < n < 20:
        return many
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def _scenes_usage_label(count: int) -> str:
    if count <= 0:
        return "Не используется в сценах"
    return f"Используется в {count} {_plural_ru(count, 'сцене', 'сценах', 'сценах')}"


def _absolute_url(request, file_field, *, project=None) -> Optional[str]:
    return signed_url_for_file(file_field, request, project=project)


def _selected_poster_url(project: Project, request=None) -> Optional[str]:
    """Return the canonical selected poster variant without exposing other jobs."""
    try:
        selected = project.poster.selected_variant
    except ObjectDoesNotExist:
        return None
    if selected is None or selected.is_deleted:
        return None
    return _absolute_url(request, selected.image, project=project)


def _absolute_url_str(
    request,
    raw_url: Optional[str],
    *,
    storage_key: Optional[str] = None,
    project=None,
) -> Optional[str]:
    return signed_url_for_asset(
        storage_key=storage_key,
        legacy_url=raw_url,
        request=request,
        project=project,
    )


# --------------------------------------------------------------------------- #
# Team / user helpers
# --------------------------------------------------------------------------- #

def _user_profile_map(user_ids):
    if UserProfile is None or not user_ids:
        return {}
    return {p.user_id: p for p in UserProfile.objects.filter(user_id__in=user_ids)}


def _user_initials(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "?"
    parts = [p for p in name.split() if p]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return name[:1].upper()


_ACCESS_ROLE_LABELS = {
    ProjectMemberRole.OWNER: "Владелец",
    ProjectMemberRole.ADMIN: "Администратор",
    ProjectMemberRole.EDITOR: "Редактор",
    ProjectMemberRole.VIEWER: "Наблюдатель",
}


def _team_member_payload(member: ProjectMember, profile, request) -> dict:
    user = member.user
    display_name = ""
    avatar_url = None
    if profile is not None:
        display_name = (
            getattr(profile, "display_name", "")
            or getattr(profile, "public_username", "")
            or ""
        )
        if getattr(profile, "avatar", None):
            avatar_url = _absolute_url(request, profile.avatar)
    if not display_name:
        display_name = user.username or user.email or f"User #{user.id}"
    return {
        "id": user.id,
        "displayName": display_name,
        "avatarUrl": avatar_url,
        "initials": _user_initials(display_name),
        "role": member.role,
        "roleLabel": _ACCESS_ROLE_LABELS.get(member.role, ""),
        "teamRole": member.team_role or "",
        "teamRoleLabel": member.team_role_label(),
        "isOwner": member.role == ProjectMemberRole.OWNER,
    }


# --------------------------------------------------------------------------- #
# Hero / project section
# --------------------------------------------------------------------------- #

def _resolve_user_role(project: Project, user: Optional[User]) -> str:
    """Return the central policy role, defaulting to viewer for display."""
    from w_craft_back.movie.project import policy

    return policy.get_role(user, project) or "viewer"


def _hero_payload(project: Project, request, user: Optional[User] = None) -> dict:
    tags = list(
        ProjectTag.objects.filter(project=project)
        .order_by("created_at")
        .values_list("name", flat=True)
    )

    all_members = list(
        ProjectMember.objects.filter(project=project)
        .select_related("user")
        .order_by("created_at")
    )
    member_count = len(all_members)
    members = all_members[:5]
    profiles = _user_profile_map([m.user_id for m in members])
    team_members = [
        _team_member_payload(m, profiles.get(m.user_id), request) for m in members
    ]
    owner_member = next(
        (m for m in all_members if m.role == ProjectMemberRole.OWNER), None
    )
    owner_name = None
    if owner_member is not None:
        owner_payload = _team_member_payload(
            owner_member, profiles.get(owner_member.user_id), request
        )
        owner_name = owner_payload["displayName"]

    description = project.description or project.desc or ""
    cover_url = (
        _absolute_url(request, project.cover_image, project=project)
        or _selected_poster_url(project, request)
        or _absolute_url(request, project.image, project=project)
    )

    from w_craft_back.movie.project import policy as _policy

    role = _resolve_user_role(project, user)
    return {
        "id": project.id,
        "title": project.title,
        "subtitle": "Страница проекта",
        "description": description,
        "status": project.status,
        "statusLabel": _STATUS_LABELS.get(project.status, project.status),
        "coverImageUrl": cover_url,
        "isFavorite": bool(project.is_favorite),
        "updatedAt": project.updated_at.isoformat() if project.updated_at else None,
        "updatedAtLabel": _format_relative_ru(project.updated_at, prefix="Обновлено "),
        "tags": tags,
        "teamMembers": team_members,
        "memberCount": member_count,
        "ownerName": owner_name,
        "isTeamProject": role != ProjectMemberRole.OWNER,
        "currentUserRole": role,
        "currentUserRoleLabel": _ACCESS_ROLE_LABELS.get(role, ""),
        "permissions": _policy.permission_summary(user, project),
    }


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #

def _stats_payload(project: Project) -> dict:
    visible = _visible_studio_characters(project)
    chars_total = len(visible)
    chars_active = chars_total

    scenes_total = Scene.objects.filter(project=project).count()
    scenes_completed = Scene.objects.filter(project=project, status="completed").count()

    music_total = MusicTrack.objects.filter(project=project).count()
    music_used = (
        SceneMusic.objects.filter(track__project=project)
        .values("track_id")
        .distinct()
        .count()
    )

    locations_total = Location.objects.filter(project=project).count()
    locations_created = Location.objects.filter(project=project, is_created=True).count()

    return {
        "charactersTotal": chars_total,
        "charactersActive": chars_active,
        "scenesTotal": scenes_total,
        "scenesCompleted": scenes_completed,
        "musicTotal": music_total,
        "musicUsed": music_used,
        "locationsTotal": locations_total,
        "locationsCreated": locations_created,
    }


# --------------------------------------------------------------------------- #
# Characters
# --------------------------------------------------------------------------- #

_CHARACTER_ROLE_ORDER = {
    CharacterRole.MAIN: 0,
    CharacterRole.ANTAGONIST: 1,
    CharacterRole.SECONDARY: 2,
    CharacterRole.EPISODIC: 3,
    CharacterRole.CAMEO: 4,
}


def _visible_studio_characters(project: Project) -> list[StudioCharacter]:
    """Return characters that should appear on the project dashboard.

    Dedupes by lowercased name; among duplicates, prefers the row that has a
    canonical reference image and the freshest updated_at.
    """

    rows = list(
        StudioCharacter.objects.filter(project=project)
        .select_related("canonical_reference_image")
    )

    def quality(c: StudioCharacter) -> tuple[int, float]:
        return (
            1 if c.canonical_reference_image_id else 0,
            (c.updated_at.timestamp() if c.updated_at else 0.0),
        )

    by_name: dict[str, StudioCharacter] = {}
    for c in rows:
        key = (c.name or "").strip().lower()
        if not key:
            by_name[str(c.character_id)] = c
            continue
        existing = by_name.get(key)
        if existing is None or quality(c) > quality(existing):
            by_name[key] = c

    return list(by_name.values())


def _characters_payload(project: Project, request, limit: int = 6) -> list[dict]:
    visible = _visible_studio_characters(project)

    def sort_key(c: StudioCharacter):
        role_rank = _CHARACTER_ROLE_ORDER.get(c.role, 99)
        return (role_rank, -(c.updated_at.timestamp() if c.updated_at else 0))

    visible.sort(key=sort_key)

    out: list[dict] = []
    for c in visible[:limit]:
        avatar_url = None
        main_image_url = None
        ref = c.canonical_reference_image
        if ref is not None:
            avatar_url = _absolute_url_str(
                request,
                getattr(ref, "image_url", None),
                storage_key=getattr(ref, "storage_path", None),
                project=project,
            )
            main_image_url = avatar_url
        out.append(
            {
                "id": str(c.character_id),
                "name": c.name,
                "role": c.role or "",
                "roleLabel": _CHARACTER_ROLE_LABELS.get(c.role, ""),
                "shortDescription": c.short_description or "",
                "avatarImageUrl": avatar_url,
                "mainImageUrl": main_image_url,
                "isActive": True,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def _pipeline_payload(project: Project, scenes_total: int) -> dict:
    from w_craft_back.movie.reference_library.models import ProjectReference

    progress = getattr(project, "progress", None)
    script_p = _clamp_progress(getattr(progress, "script_progress", 0))
    visual_p = _clamp_progress(getattr(progress, "visual_progress", 0))
    postprod_p = _clamp_progress(getattr(progress, "postproduction_progress", 0))

    # Group all four asset-type counts into a single aggregate query (was 4
    # separate counts firing every dashboard load).
    asset_counts = (
        ProjectAsset.objects.filter(
            project=project,
            asset_type__in=("storyboard", "reference", "model_3d", "video"),
        )
        .values_list("asset_type")
        .annotate(c=Count("id"))
    )
    counts_by_type = {row[0]: row[1] for row in asset_counts}
    storyboard_count = counts_by_type.get("storyboard", 0)
    reference_count = ProjectReference.objects.filter(
        project=project,
        archived_at__isnull=True,
        active_version__isnull=False,
    ).count()
    models3d_count = counts_by_type.get("model_3d", 0)
    video_count = counts_by_type.get("video", 0)

    def _pluralize_scenes(n):
        return f"{n} {_plural_ru(n, 'сцена', 'сцены', 'сцен')}"

    def _pluralize_files(n):
        return f"{n} {_plural_ru(n, 'файл', 'файла', 'файлов')}"

    def _pluralize_references(n):
        return f"{n} {_plural_ru(n, 'опора', 'опоры', 'опор')}"

    def _pluralize_models(n):
        return f"{n} {_plural_ru(n, 'модель', 'модели', 'моделей')}"

    return {
        "script": {
            "label": "Сценарий",
            "progress": script_p,
            "subtitle": _pluralize_scenes(scenes_total),
        },
        "storyboard": {
            "label": "Сториборд",
            # TODO(progress): split visual into storyboard/reference/3d separately later
            "progress": visual_p,
            "subtitle": _pluralize_scenes(storyboard_count),
        },
        "references": {
            "label": "Опорные изображения",
            "progress": visual_p,
            "subtitle": _pluralize_references(reference_count),
        },
        "models3d": {
            "label": "3D",
            "progress": visual_p,
            "subtitle": _pluralize_models(models3d_count),
        },
        "video": {
            "label": "Видео",
            "progress": postprod_p,
            "subtitle": _pluralize_scenes(video_count),
        },
    }


# --------------------------------------------------------------------------- #
# Music
# --------------------------------------------------------------------------- #

def _signed_music_expiry(audio_url: Optional[str]) -> Optional[str]:
    if not audio_url:
        return None
    ttl_seconds = max(1, int(getattr(settings, "SIGNED_MEDIA_TTL_SECONDS", 300)))
    return (timezone.now() + timedelta(seconds=ttl_seconds)).isoformat()


def _music_payload(project: Project, request, limit: int = 5) -> list[dict]:
    tracks = list(
        MusicTrack.objects.filter(project=project, archived_at__isnull=True)
        .select_related("active_version__asset__project")
        .annotate(usage_count=Count("scene_usages"))
        .order_by("-usage_count", "-updated_at")[:limit]
    )
    out = []
    for t in tracks:
        usage = int(getattr(t, "usage_count", 0) or 0)
        active_version = t.active_version
        if active_version is not None:
            asset = active_version.asset
            audio_url = signed_url_for_music_asset(asset, request=request)
            duration_seconds = (
                float(asset.duration_seconds)
                if asset.duration_seconds is not None
                else float(t.duration_seconds or 0)
            )
            active_version_id = str(active_version.id)
            active_version_number = active_version.version_number
        else:
            audio_url = _absolute_url(
                request,
                t.audio_file,
                project=project,
            )
            duration_seconds = float(t.duration_seconds or 0)
            active_version_id = None
            active_version_number = None
        audio_url_expires_at = _signed_music_expiry(audio_url)
        active_version_payload = (
            {
                "versionId": active_version_id,
                "versionNumber": active_version_number,
                "durationSeconds": duration_seconds,
                "audioUrl": audio_url,
                "audioUrlExpiresAt": audio_url_expires_at,
            }
            if active_version is not None
            else None
        )
        out.append(
            {
                "id": t.id,
                "title": t.title,
                "author": t.author or "",
                "durationSeconds": duration_seconds,
                "durationLabel": _format_duration(duration_seconds),
                "tags": list(t.tags or []),
                "coverImageUrl": _absolute_url(
                    request,
                    t.cover_image,
                    project=project,
                ),
                "audioUrl": audio_url,
                "audioUrlExpiresAt": audio_url_expires_at,
                "activeVersionId": active_version_id,
                "activeVersionNumber": active_version_number,
                "activeVersion": active_version_payload,
                "version": t.version,
                "source": t.source,
                "usageCount": usage,
                "usageLabel": _scenes_usage_label(usage),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Progress card
# --------------------------------------------------------------------------- #

def _progress_payload(project: Project) -> dict:
    progress = getattr(project, "progress", None)
    return {
        "overall": _clamp_progress(getattr(progress, "overall_progress", 0)),
        "script": _clamp_progress(getattr(progress, "script_progress", 0)),
        "visual": _clamp_progress(getattr(progress, "visual_progress", 0)),
        "audio": _clamp_progress(getattr(progress, "audio_progress", 0)),
        "postproduction": _clamp_progress(getattr(progress, "postproduction_progress", 0)),
    }


# --------------------------------------------------------------------------- #
# Quick actions
# --------------------------------------------------------------------------- #

def _quick_actions_payload(project: Project) -> list[dict]:
    base = f"/project-list/project"
    return [
        {"key": "new_scene", "label": "Новая сцена", "url": f"{base}/scenes/create"},
        {
            "key": "upload_reference",
            "label": "Создать опору",
            "url": f"/project/{project.id}/references/create",
        },
        {"key": "generate_video", "label": "Генерация видео", "url": f"{base}/generate-video"},
        {"key": "create_location", "label": "Создать локацию", "url": f"{base}/locations/create"},
    ]


# --------------------------------------------------------------------------- #
# Recent activity
# --------------------------------------------------------------------------- #

def _activity_payload(project: Project, request, limit: int = 5) -> list[dict]:
    qs = list(
        ProjectActivity.objects.filter(project=project)
        .select_related("user")
        .order_by("-created_at")[:limit]
    )
    out = []
    for a in qs:
        metadata = a.metadata or {}
        thumb = metadata.get("thumbnail_url") if isinstance(metadata, dict) else None
        out.append(
            {
                "id": a.id,
                "type": a.activity_type,
                "title": a.title,
                "description": a.description or "",
                "createdAt": a.created_at.isoformat() if a.created_at else None,
                "createdAtLabel": _format_relative_ru(a.created_at),
                "thumbnailUrl": thumb,
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Public service entrypoint
# --------------------------------------------------------------------------- #

def build_project_dashboard(project: Project, user: User, request=None) -> dict[str, Any]:
    hero = _hero_payload(project, request, user=user)
    stats = _stats_payload(project)
    pipeline = _pipeline_payload(project, scenes_total=stats["scenesTotal"])
    return {
        "project": hero,
        "stats": stats,
        "characters": _characters_payload(project, request),
        "pipeline": pipeline,
        "music": _music_payload(project, request),
        "progress": _progress_payload(project),
        "quickActions": _quick_actions_payload(project),
        "recentActivity": _activity_payload(project, request),
    }


def build_project_summary(project: Project, request=None, user=None) -> dict[str, Any]:
    """Compact summary used by GET /api/projects/ list endpoint and PATCH responses.

    For list responses the caller annotates ``_chars_total`` / ``_scenes_total``
    and prefetches ``tags`` + ``members`` onto each project so this function does
    zero extra queries per row (no N+1). For one-off calls (PATCH response, etc.)
    we fall back to direct count/list queries.

    When ``user`` is supplied the summary also carries the team-collaboration
    fields the "My Projects" cards need: the current user's role, the member
    count, a compact avatar list, and whether this is a team project (the user
    is not the owner).
    """

    chars_total = getattr(project, "_chars_total", None)
    if chars_total is None:
        chars_total = StudioCharacter.objects.filter(project=project).count()

    scenes_total = getattr(project, "_scenes_total", None)
    if scenes_total is None:
        scenes_total = Scene.objects.filter(project=project).count()

    cover_url = (
        _absolute_url(request, project.cover_image, project=project)
        or _selected_poster_url(project, request)
        or _absolute_url(request, project.image, project=project)
    )

    # When the caller did a ``prefetch_related('tags')`` we read the cache
    # directly; otherwise fall back to a fresh query. Tag ordering matches
    # the original (by created_at).
    prefetched = getattr(project, "_prefetched_objects_cache", {}).get("tags")
    if prefetched is not None:
        tags = [t.name for t in sorted(prefetched, key=lambda t: t.created_at)]
    else:
        tags = list(
            ProjectTag.objects.filter(project=project)
            .order_by("created_at")
            .values_list("name", flat=True)
        )

    payload = {
        "id": project.id,
        "title": project.title,
        "description": project.description or project.desc or "",
        "status": project.status,
        "statusLabel": _STATUS_LABELS.get(project.status, project.status),
        "coverImageUrl": cover_url,
        "updatedAt": project.updated_at.isoformat() if project.updated_at else None,
        "updatedAtLabel": _format_relative_ru(project.updated_at, prefix="Обновлено "),
        "isFavorite": bool(project.is_favorite),
        "tags": tags,
        "stats": {
            "charactersTotal": chars_total,
            "scenesTotal": scenes_total,
        },
    }

    # Team-collaboration fields for the "My Projects" cards. Only emitted when a
    # user is known (the list endpoint passes it; legacy callers may not).
    if user is not None:
        # Prefer the prefetched members cache to avoid an N+1 over the list.
        members_cache = getattr(project, "_prefetched_objects_cache", {}).get("members")
        if members_cache is not None:
            members = list(members_cache)
        else:
            members = list(
                ProjectMember.objects.filter(project=project).select_related("user")
            )
        member_count = len(members)
        # Role: owner via FK/legacy, else the matching member row.
        role = _resolve_user_role(project, user)
        # Compact avatar list (first few members).
        ordered = sorted(
            members, key=lambda m: (m.created_at or m.id)
        ) if members else []
        profiles = _user_profile_map([m.user_id for m in ordered[:5]])
        team_members = [
            _team_member_payload(m, profiles.get(m.user_id), request)
            for m in ordered[:5]
        ]
        owner_member = next(
            (m for m in members if m.role == ProjectMemberRole.OWNER), None
        )
        payload.update(
            {
                "currentUserRole": role,
                "currentUserRoleLabel": _ACCESS_ROLE_LABELS.get(role, ""),
                "memberCount": member_count,
                "teamMembers": team_members,
                "isTeamProject": role != ProjectMemberRole.OWNER,
                "ownerUserId": owner_member.user_id if owner_member else None,
            }
        )

    return payload


def build_project_edit_payload(project: Project, request=None) -> dict[str, Any]:
    """Full payload for the project settings/edit page.

    Includes editor-specific fields (format, genre, audience, annotation,
    synopsis, posterUrl) on top of the regular summary so the frontend can
    populate the form from a single GET call.
    """

    base = build_project_summary(project, request)
    poster_url = (
        _selected_poster_url(project, request)
        or _absolute_url(request, project.image, project=project)
        or _absolute_url(request, project.cover_image, project=project)
    )
    base.update({
        "format": project.format or "",
        "genre": [g.translit for g in project.genre.all()],
        # Stable English values (e.g. "kids") so the frontend chips can match
        # by value, not by display label.
        "audience": [a.translit for a in project.audience.all() if a.translit],
        "annotation": project.annot or "",
        "synopsis": project.desc or project.description or "",
        "posterUrl": poster_url,
        "generationSettings": dict(project.generation_settings or {}),
        "createdAt": project.created_at.isoformat() if project.created_at else None,
    })
    return base


# --------------------------------------------------------------------------- #
# Activity helper (used by CRUD action views)
# --------------------------------------------------------------------------- #

def record_activity(
    project: Project,
    user: Optional[User],
    activity_type: str,
    title: str,
    description: str = "",
    metadata: Optional[dict] = None,
    target_type: str = "",
    target_id: str = "",
) -> ProjectActivity:
    return ProjectActivity.objects.create(
        project=project,
        user=user,
        activity_type=activity_type,
        title=title,
        description=description or "",
        metadata=metadata or {},
        target_type=target_type or "",
        target_id=target_id or "",
    )
