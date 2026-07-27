"""Permission-checked mutation boundary for project-owned aggregates.

Views validate HTTP payloads and serialize responses. This module owns project
action checks, transaction boundaries, actor attribution, and cross-project
lookups for writes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max, Model

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.project import policy
from w_craft_back.movie.project.dashboard_models import (
    Location,
    MusicTrack,
    ProjectAsset,
    ProjectGenerationJob,
    ProjectGenerationJobStatus,
    ProjectTag,
    Scene,
)
from w_craft_back.movie.project.models import Project, ProjectStatus
from w_craft_back.movie.project.services import record_activity
from w_craft_back.movie.project.script_workspace import (
    replace_scene_characters,
    script_text_from_blocks,
)
from w_craft_back.storage_gateway import (
    StorageGatewayError,
    delete_storage_key,
    store_project_upload,
)

logger = logging.getLogger(__name__)


class ProjectMutationForbidden(Exception):
    """The actor lacks the requested project action."""


class VersionConflict(Exception):
    """An optimistic-lock version no longer matches the stored entity."""

    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"Current version is {current_version}")


def get_project_for_action(
    *,
    actor: User,
    project_id: int,
    action: policy.Action,
    lock: bool = False,
) -> Project:
    """Return a project only when ``actor`` may perform ``action``."""
    queryset = Project.objects.select_related("owner", "user")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    project = queryset.get(pk=project_id)
    if not policy.can(actor, project, action):
        raise ProjectMutationForbidden
    return project


def _require_action(action: policy.Action, expected: policy.Action) -> None:
    if action is not expected:
        raise ValueError(
            f"This mutation requires {expected.value}, received {action.value}"
        )


def _replace_tags(project: Project, names: Sequence[str]) -> None:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        normalized = name.lower()
        if not name or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(name)
    ProjectTag.objects.filter(project=project).delete()
    ProjectTag.objects.bulk_create(
        [ProjectTag(project=project, name=name) for name in cleaned]
    )


def _delete_file_after_commit(file_field) -> None:
    name = getattr(file_field, "name", "")
    storage = getattr(file_field, "storage", None)
    if not name or storage is None:
        return

    def delete_old_file() -> None:
        try:
            storage.delete(name)
        except Exception:  # pragma: no cover - storage race/failure
            logger.warning("Failed to delete replaced project poster", exc_info=True)

    transaction.on_commit(delete_old_file)


@transaction.atomic
def update_project_settings(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    data: Mapping[str, Any],
    genres: Sequence[Model] | None = None,
    audiences: Sequence[Model] | None = None,
    poster_file: ContentFile | None = None,
    poster_supplied: bool = False,
) -> Project:
    """Update project metadata under the explicit ``EDIT_SETTINGS`` action."""
    _require_action(action, policy.Action.EDIT_SETTINGS)
    project = get_project_for_action(
        actor=actor,
        project_id=project_id,
        action=action,
        lock=True,
    )
    previous_status = project.status

    for field in (
        "title",
        "description",
        "status",
        "is_favorite",
        "generation_settings",
    ):
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

    if "status" in data:
        from django.utils import timezone

        if data["status"] == ProjectStatus.ARCHIVED and project.archived_at is None:
            project.archived_at = timezone.now()
        elif (
            data["status"] != ProjectStatus.ARCHIVED
            and project.archived_at is not None
        ):
            project.archived_at = None

    if poster_supplied:
        old_poster = project.image
        if poster_file is None:
            project.image = ""
        else:
            project.image.save(poster_file.name, poster_file, save=False)
        _delete_file_after_commit(old_poster)

    project.save()

    if "tags" in data:
        _replace_tags(project, data["tags"])
    if "genre" in data:
        project.genre.set(genres or [])
    if "audience" in data:
        project.audience.set(audiences or [])

    new_status = data.get("status")
    status_changed = new_status is not None and new_status != previous_status
    editor_fields = (
        "format",
        "genre",
        "audience",
        "annotation",
        "synopsis",
        "poster_image_data",
        "poster_url",
    )
    non_status_changed = any(
        field in data
        for field in (
            "title",
            "description",
            "is_favorite",
            "tags",
            "generation_settings",
        ) + editor_fields
    )

    if status_changed and new_status == ProjectStatus.ARCHIVED:
        record_activity(
            project,
            actor,
            "project_archived",
            title=project.title,
            description="проект архивирован",
            metadata={"from": previous_status, "to": new_status},
        )
    elif status_changed:
        record_activity(
            project,
            actor,
            "project_status_changed",
            title=project.title,
            description="статус проекта изменён",
            metadata={"from": previous_status, "to": new_status},
        )

    if non_status_changed:
        record_activity(
            project,
            actor,
            "project_updated",
            title=project.title,
            description="проект обновлён",
        )
    return project


@transaction.atomic
def create_project_character(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    data: Mapping[str, Any],
) -> StudioCharacter:
    """Create a canonical character owned by the project."""
    _require_action(action, policy.Action.EDIT_CONTENT)
    project = get_project_for_action(
        actor=actor, project_id=project_id, action=action, lock=True
    )
    actor_key = UserKey.objects.filter(user=actor).first()
    character = StudioCharacter.objects.create(
        project=project,
        user=actor_key,
        name=data["name"],
        short_description=data.get("short_description", ""),
        role=data.get("role", "secondary"),
        status="active",
    )
    record_activity(
        project,
        actor,
        "character_created",
        title=character.name,
        description="персонаж создан",
        metadata={"character_id": str(character.character_id)},
    )
    return character


def _validate_scene_order(project: Project, order: int, *, exclude_id=None) -> None:
    if order < 1:
        raise ValidationError({"order": ["order must be at least 1"]})
    orders = Scene.objects.filter(project=project, order=order)
    if exclude_id is not None:
        orders = orders.exclude(pk=exclude_id)
    if orders.exists():
        raise ValidationError(
            {"order": ["another scene already uses this order in the project"]}
        )


@transaction.atomic
def create_scene(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    data: Mapping[str, Any],
) -> Scene:
    """Create a scene with serialized project-local order allocation."""
    _require_action(action, policy.Action.EDIT_CONTENT)
    project = get_project_for_action(
        actor=actor, project_id=project_id, action=action, lock=True
    )
    location = None
    location_id = data.get("location_id")
    if location_id:
        location = Location.objects.filter(pk=location_id, project=project).first()
        if location is None:
            raise ValidationError(
                {"location_id": ["location not found in this project"]}
            )

    requested_order = data.get("order")
    if requested_order is None:
        highest = (
            Scene.objects.filter(project=project).aggregate(value=Max("order"))["value"]
            or 0
        )
        order = highest + 1
    else:
        order = int(requested_order)
        _validate_scene_order(project, order)

    script_blocks = data.get("script_blocks")
    script_text = data.get("script_text", "")
    if script_blocks is not None:
        script_text = script_text_from_blocks(script_blocks)

    scene = Scene.objects.create(
        project=project,
        title=data["title"],
        description=data.get("description", ""),
        script_text=script_text,
        script_blocks=script_blocks or [],
        status=data.get("status", "draft"),
        act=data.get("act", 1),
        duration_seconds=data.get("duration_seconds", 0),
        mood=data.get("mood", ""),
        scene_type=data.get("scene_type", "other"),
        notes=data.get("notes", ""),
        camera_settings=data.get("camera_settings", {}),
        location=location,
        order=order,
        created_by=actor,
        updated_by=actor,
    )
    replace_scene_characters(scene, project, data.get("character_ids", []))
    record_activity(
        project,
        actor,
        "scene_created",
        title=scene.title,
        description="сцена создана",
        metadata={"scene_id": scene.id},
    )
    return scene


@transaction.atomic
def create_music_track(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    data: Mapping[str, Any],
) -> MusicTrack:
    """Create a project-owned music track."""
    _require_action(action, policy.Action.EDIT_CONTENT)
    project = get_project_for_action(
        actor=actor, project_id=project_id, action=action, lock=True
    )
    track = MusicTrack.objects.create(
        project=project,
        title=data["title"],
        author=data.get("author", ""),
        duration_seconds=data.get("duration_seconds", 0),
        tags=data.get("tags", []),
        created_by=actor,
        updated_by=actor,
    )
    record_activity(
        project,
        actor,
        "music_added",
        title=track.title,
        description=track.author or "",
        metadata={"track_id": track.id},
    )
    return track


@transaction.atomic
def create_location(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    data: Mapping[str, Any],
) -> Location:
    """Create a project-owned location."""
    _require_action(action, policy.Action.EDIT_CONTENT)
    project = get_project_for_action(
        actor=actor, project_id=project_id, action=action, lock=True
    )
    location = Location.objects.create(
        project=project,
        name=data["name"],
        description=data.get("description", ""),
        created_by=actor,
        updated_by=actor,
    )
    record_activity(
        project,
        actor,
        "location_created",
        title=location.name,
        description="локация создана",
        metadata={"location_id": location.id},
    )
    return location


def create_project_asset(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    upload,
    asset_type: str,
    title: str,
) -> ProjectAsset:
    """Validate/store a project-owned asset outside the metadata transaction."""

    _require_action(action, policy.Action.EDIT_CONTENT)
    project = get_project_for_action(
        actor=actor,
        project_id=project_id,
        action=action,
    )
    try:
        stored = store_project_upload(
            upload,
            project_id=project.id,
            asset_type=asset_type,
        )
    except StorageGatewayError as exc:
        raise ValidationError({"file": [exc.message]}) from exc

    try:
        with transaction.atomic():
            project = get_project_for_action(
                actor=actor,
                project_id=project_id,
                action=action,
                lock=True,
            )
            asset = ProjectAsset(
                project=project,
                uploaded_by=actor,
                asset_type=asset_type,
                title=title,
                metadata={
                    "mime_type": stored.mime_type,
                    "size_bytes": stored.size_bytes,
                    "sha256": stored.sha256,
                    "width": stored.width,
                    "height": stored.height,
                },
            )
            asset.file.name = stored.storage_key
            asset.save()
            record_activity(
                project,
                actor,
                "asset_uploaded",
                title=title or f"Asset {asset.id}",
                description=asset_type,
                metadata={"asset_id": asset.id, "asset_type": asset_type},
            )
    except Exception:
        delete_storage_key(stored.storage_key)
        raise
    return asset


@transaction.atomic
def enqueue_project_generation(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    data: Mapping[str, Any],
) -> ProjectGenerationJob:
    """Create a billable project job under ``RUN_GENERATION``."""
    _require_action(action, policy.Action.RUN_GENERATION)
    project = get_project_for_action(
        actor=actor, project_id=project_id, action=action, lock=True
    )
    return ProjectGenerationJob.objects.create(
        project=project,
        user=actor,
        job_type=data["job_type"],
        status=ProjectGenerationJobStatus.QUEUED,
        prompt=data.get("prompt", ""),
        negative_prompt=data.get("negative_prompt", ""),
        input_data=data.get("input_data", {}),
    )


@transaction.atomic
def update_versioned_entity(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    model,
    object_id,
    expected_version: int | None,
    changes: Mapping[str, Any],
):
    """Update a project entity with policy and optimistic locking."""
    _require_action(action, policy.Action.EDIT_CONTENT)
    project = get_project_for_action(
        actor=actor, project_id=project_id, action=action, lock=True
    )
    entity = (
        model.objects.select_for_update()
        .filter(pk=object_id, project=project)
        .first()
    )
    if entity is None:
        raise model.DoesNotExist
    if expected_version is not None and expected_version != entity.version:
        raise VersionConflict(entity.version)

    for field, value in changes.items():
        setattr(entity, field, value)
    if changes:
        entity.version = (entity.version or 1) + 1
        if hasattr(entity, "updated_by_id"):
            entity.updated_by = actor
        entity.save()
    return entity


@transaction.atomic
def update_scene(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    scene_id: int,
    expected_version: int,
    data: Mapping[str, Any],
    character_ids=None,
    location_supplied: bool = False,
    location_id=None,
) -> Scene:
    """Update a scene and its project-local links atomically."""
    _require_action(action, policy.Action.EDIT_CONTENT)
    project = get_project_for_action(
        actor=actor, project_id=project_id, action=action, lock=True
    )
    scene = (
        Scene.objects.select_for_update()
        .filter(pk=scene_id, project=project)
        .first()
    )
    if scene is None:
        raise Scene.DoesNotExist
    if expected_version != scene.version:
        raise VersionConflict(scene.version)

    changes = dict(data)
    if "order" in changes:
        _validate_scene_order(
            project,
            int(changes["order"]),
            exclude_id=scene.pk,
        )
    if "script_blocks" in changes:
        changes["script_text"] = script_text_from_blocks(changes["script_blocks"])
    elif "script_text" in changes:
        changes["script_blocks"] = []

    location = None
    if location_supplied and location_id is not None:
        location = Location.objects.filter(pk=location_id, project=project).first()
        if location is None:
            raise ValidationError(
                {"location_id": ["location not found in this project"]}
            )

    changed = bool(changes) or character_ids is not None or location_supplied
    for field, value in changes.items():
        setattr(scene, field, value)
    if location_supplied:
        scene.location = location
    if character_ids is not None:
        replace_scene_characters(scene, project, character_ids)
    if changed:
        scene.version = (scene.version or 1) + 1
        scene.updated_by = actor
        scene.save()
    return scene


@transaction.atomic
def delete_project_entity(
    *,
    actor: User,
    action: policy.Action,
    project_id: int,
    model,
    object_id,
) -> None:
    """Delete a project-owned entity after a scoped lookup and action check."""
    _require_action(action, policy.Action.EDIT_CONTENT)
    project = get_project_for_action(
        actor=actor, project_id=project_id, action=action, lock=True
    )
    entity = (
        model.objects.select_for_update()
        .filter(pk=object_id, project=project)
        .first()
    )
    if entity is None:
        raise model.DoesNotExist
    entity.delete()
