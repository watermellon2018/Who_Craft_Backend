"""Serialization and relationship helpers for the script workspace API."""

from __future__ import annotations

from typing import Iterable, Optional

from django.db.models import Count, Prefetch

from w_craft_back.character_studio.models import (
    CharacterRole,
    StudioCharacter,
    VISIBLE_CHARACTER_STATUSES,
)
from w_craft_back.movie.project.dashboard_models import Scene, SceneCharacter
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.progress_service import analyze_missing_characters
from w_craft_back.storage_gateway import signed_url_for_asset


CHARACTER_ROLE_LABELS = dict(CharacterRole.choices)


def missing_characters_payload(project: Project) -> dict:
    return {
        "characters": [
            character.as_payload()
            for character in analyze_missing_characters(project)
        ]
    }


def _absolute_image_url(request, raw_url: Optional[str]) -> Optional[str]:
    if not raw_url:
        return None
    normalized = raw_url.strip()
    if not normalized:
        return None
    if normalized.startswith(("http://", "https://", "//")):
        return normalized
    return request.build_absolute_uri(normalized) if request is not None else normalized


def character_image_url(character: StudioCharacter, request) -> Optional[str]:
    reference = character.canonical_reference_image
    if reference is None:
        return None
    return signed_url_for_asset(
        storage_key=reference.storage_path,
        legacy_url=reference.image_url,
        request=request,
        project=character.project,
    )


def compact_character_payload(character: StudioCharacter, request) -> dict:
    return {
        "id": str(character.character_id),
        "name": character.name,
        "role": character.role or "",
        "roleLabel": CHARACTER_ROLE_LABELS.get(character.role, ""),
        "imageUrl": character_image_url(character, request),
    }


def scene_script_blocks(scene: Scene) -> list[dict]:
    blocks = scene.script_blocks if isinstance(scene.script_blocks, list) else []
    if blocks:
        return blocks
    if scene.script_text:
        return [
            {
                "id": f"legacy-{scene.pk}",
                "type": "action",
                "text": scene.script_text,
            }
        ]
    return []


def script_text_from_blocks(blocks: Iterable[dict]) -> str:
    return "\n".join(
        block["text"]
        for block in blocks
        if isinstance(block.get("text"), str) and block["text"]
    )


def scene_payload(scene: Scene, request) -> dict:
    links = list(scene.scene_characters.all())
    return {
        "id": scene.id,
        "title": scene.title,
        "description": scene.description,
        "scriptText": scene.script_text,
        "scriptBlocks": scene_script_blocks(scene),
        "status": scene.status,
        "order": scene.order,
        "act": scene.act,
        "durationSeconds": scene.duration_seconds,
        "mood": scene.mood,
        "sceneType": scene.scene_type,
        "notes": scene.notes,
        "cameraSettings": scene.camera_settings or {},
        "characters": [
            compact_character_payload(link.character, request) for link in links
        ],
        "version": scene.version,
        "updatedAt": scene.updated_at.isoformat() if scene.updated_at else None,
        "updatedById": scene.updated_by_id,
        "updatedByUsername": (
            scene.updated_by.username if scene.updated_by_id else None
        ),
    }


def scenes_queryset(project: Project):
    character_links = SceneCharacter.objects.select_related(
        "character__canonical_reference_image"
    ).order_by("id")
    return (
        Scene.objects.filter(project=project)
        .select_related("updated_by")
        .prefetch_related(
            Prefetch("scene_characters", queryset=character_links)
        )
    )


def scenes_collection_payload(project: Project, user, request) -> dict:
    from w_craft_back.movie.project.policy import permission_summary

    scenes = list(scenes_queryset(project))
    acts = [
        {
            "act": act,
            "sceneCount": sum(scene.act == act for scene in scenes),
            "durationSeconds": sum(
                scene.duration_seconds for scene in scenes if scene.act == act
            ),
        }
        for act in (1, 2, 3)
    ]
    return {
        "project": {
            "id": project.id,
            "title": project.title,
            "permissions": permission_summary(user, project),
        },
        "stats": {
            "sceneCount": len(scenes),
            "totalDurationSeconds": sum(scene.duration_seconds for scene in scenes),
            "acts": acts,
        },
        "scenes": [scene_payload(scene, request) for scene in scenes],
    }


def characters_collection_payload(project: Project, request) -> dict:
    characters = list(
        StudioCharacter.objects.filter(
            project=project,
            status__in=VISIBLE_CHARACTER_STATUSES,
        )
        .select_related("canonical_reference_image")
        .prefetch_related("scene_appearances")
        .annotate(scene_count=Count("scene_appearances", distinct=True))
        .order_by("created_at", "character_id")
    )
    return {
        "characters": [
            {
                **compact_character_payload(character, request),
                "shortDescription": character.short_description or "",
                "personality": character.personality or {},
                "backstory": character.backstory or "",
                "speechStyle": character.speech_style or "",
                "sceneCount": character.scene_count,
                "sceneIds": sorted(
                    {
                        appearance.scene_id
                        for appearance in character.scene_appearances.all()
                    }
                ),
            }
            for character in characters
        ]
    }


def replace_scene_characters(
    scene: Scene,
    project: Project,
    character_ids: Iterable,
) -> None:
    characters_by_id = {
        character.character_id: character
        for character in StudioCharacter.objects.filter(
            project=project,
            character_id__in=character_ids,
        )
    }
    SceneCharacter.objects.filter(scene=scene).delete()
    SceneCharacter.objects.bulk_create(
        [
            SceneCharacter(scene=scene, character=characters_by_id[character_id])
            for character_id in character_ids
        ]
    )
