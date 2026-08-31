"""Transactional Storyboard application services and response assembly."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Prefetch

from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.project import policy
from w_craft_back.movie.project.dashboard_models import (
    Location,
    Scene,
    SceneStoryboard,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.reference_library.models import ProjectReference
from w_craft_back.movie.storyboard.domain.continuity import (
    ContinuityReferenceService,
)
from w_craft_back.movie.storyboard.domain.readiness import (
    ShotReadinessService,
)
from w_craft_back.movie.storyboard.domain.transitions import (
    rebuild_transitions,
    recalculate_adjacent_transitions,
)
from w_craft_back.movie.storyboard.errors import (
    StoryboardConflict,
    StoryboardError,
    StoryboardNotFound,
)
from w_craft_back.movie.storyboard.models import (
    CameraIntent,
    CameraTransition,
    GenerationReferenceType,
    KeyframeGenerationReference,
    StoryboardGenerationStatus,
    StoryboardKeyframe,
    StoryboardKeyframeGeneration,
    StoryboardKeyframeType,
    StoryboardShot,
    StoryboardShotCharacter,
    StoryboardShotReference,
)
from w_craft_back.movie.storyboard.validators import (
    validate_camera_metadata,
    validate_camera_target,
    validate_composition,
)
from w_craft_back.storage_gateway import signed_media_url


def _require_project(
    *,
    actor: Any,
    project_id: int,
    action: policy.Action,
) -> Project:
    project = Project.objects.filter(pk=project_id).first()
    if project is None:
        raise StoryboardNotFound("Project not found.")
    if not policy.can(actor, project, action):
        raise StoryboardError(
            "Project access is forbidden.",
            code="STORYBOARD_FORBIDDEN",
            http_status=403,
        )
    return project


def _scene(project: Project, scene_id: int, *, lock: bool = False) -> Scene:
    queryset = Scene.objects
    if lock:
        queryset = queryset.select_for_update()
    scene = queryset.filter(pk=scene_id, project=project).first()
    if scene is None:
        raise StoryboardNotFound("Scene not found.")
    return scene


def _storyboard(project: Project, storyboard_id: int) -> SceneStoryboard:
    storyboard = SceneStoryboard.objects.filter(
        pk=storyboard_id,
        scene__project=project,
    ).first()
    if storyboard is None:
        raise StoryboardNotFound()
    return storyboard


def _shot(
    project: Project,
    shot_id: uuid.UUID,
    *,
    lock: bool = False,
) -> StoryboardShot:
    queryset = StoryboardShot.objects
    if lock:
        queryset = queryset.select_for_update()
    shot = queryset.filter(
        pk=shot_id,
        storyboard__scene__project=project,
    ).first()
    if shot is None:
        raise StoryboardNotFound("Shot not found.")
    return shot


def _keyframe(
    project: Project,
    keyframe_id: uuid.UUID,
    *,
    lock: bool = False,
) -> StoryboardKeyframe:
    queryset = StoryboardKeyframe.objects
    if lock:
        queryset = queryset.select_for_update()
    keyframe = queryset.filter(
        pk=keyframe_id,
        shot__storyboard__scene__project=project,
    ).first()
    if keyframe is None:
        raise StoryboardNotFound("Keyframe not found.")
    return keyframe


def _locked_storyboard_shot(
    project: Project,
    shot_id: uuid.UUID,
) -> tuple[SceneStoryboard, StoryboardShot]:
    storyboard_id = StoryboardShot.objects.filter(
        pk=shot_id,
        storyboard__scene__project=project,
    ).values_list("storyboard_id", flat=True).first()
    if storyboard_id is None:
        raise StoryboardNotFound("Shot not found.")
    storyboard = SceneStoryboard.objects.select_for_update().get(
        pk=storyboard_id,
    )
    shots = list(
        StoryboardShot.objects.select_for_update()
        .filter(storyboard=storyboard)
        .order_by("pk")
    )
    shot = next((item for item in shots if item.pk == shot_id), None)
    if shot is None:
        raise StoryboardNotFound("Shot not found.")
    return storyboard, shot


def _locked_keyframe_set(
    project: Project,
    keyframe_id: uuid.UUID,
) -> tuple[StoryboardShot, StoryboardKeyframe, list[StoryboardKeyframe]]:
    shot_id = StoryboardKeyframe.objects.filter(
        pk=keyframe_id,
        shot__storyboard__scene__project=project,
    ).values_list("shot_id", flat=True).first()
    if shot_id is None:
        raise StoryboardNotFound("Keyframe not found.")
    shot = StoryboardShot.objects.select_for_update().filter(
        pk=shot_id,
        storyboard__scene__project=project,
    ).first()
    if shot is None:
        raise StoryboardNotFound("Keyframe not found.")
    keyframes = list(
        StoryboardKeyframe.objects.select_for_update()
        .filter(shot=shot)
        .order_by("pk")
    )
    keyframe = next((item for item in keyframes if item.pk == keyframe_id), None)
    if keyframe is None:
        raise StoryboardNotFound("Keyframe not found.")
    return shot, keyframe, keyframes


def _reject_inflight_deletion(*, generations) -> None:
    locked_generations = list(
        generations.select_for_update().order_by("pk")
    )
    if any(
        item.status == StoryboardGenerationStatus.GENERATING
        for item in locked_generations
    ):
        raise StoryboardConflict(
            "Storyboard generation is currently running.",
            code="STORYBOARD_GENERATION_IN_PROGRESS",
            retryable=True,
        )


def storyboard_queryset():
    generations = StoryboardKeyframeGeneration.objects.select_related("asset").order_by(
        "-revision"
    )
    keyframes = StoryboardKeyframe.objects.select_related(
        "camera_intent",
        "current_generation",
        "current_generation__asset",
    ).prefetch_related(
        Prefetch(
            "generations",
            queryset=generations,
            to_attr="_storyboard_generation_revisions",
        ),
        "generation_references__source_keyframe",
        "generation_references__visual_reference",
        "generation_references__character",
        "generation_references__location",
    )
    shots = StoryboardShot.objects.select_related("location").prefetch_related(
        "character_links__character",
        "visual_references__reference",
        Prefetch("keyframes", queryset=keyframes),
        "transitions",
    )
    return SceneStoryboard.objects.select_related("scene", "asset").prefetch_related(
        Prefetch("shots", queryset=shots)
    )


class SceneStoryboardContextService:
    """Reuse resolved Scene/Reference data without running extraction on read."""

    @staticmethod
    def scene_text(scene: Scene) -> str:
        if str(scene.script_text or "").strip():
            return str(scene.script_text).strip()
        blocks = scene.script_blocks if isinstance(scene.script_blocks, list) else []
        block_text = "\n".join(
            str(block.get("text") or block.get("content") or "").strip()
            for block in blocks
            if isinstance(block, Mapping)
        ).strip()
        return block_text or str(scene.description or scene.notes or "").strip()

    @classmethod
    def build(cls, scene: Scene) -> dict[str, Any]:
        characters = StudioCharacter.objects.filter(
            scene_appearances__scene=scene,
        ).distinct().order_by("name")
        locations = Location.objects.filter(project=scene.project).order_by("name")
        references = ProjectReference.objects.filter(
            project=scene.project,
            archived_at__isnull=True,
        ).order_by("title")
        return {
            "scene": {
                "id": scene.id,
                "number": scene.order,
                "title": scene.title,
                "text": cls.scene_text(scene)[:20000],
                "metadata": {
                    "act": scene.act,
                    "mood": scene.mood,
                    "sceneType": scene.scene_type,
                    "durationSeconds": scene.duration_seconds,
                },
            },
            "characters": [
                {"id": str(item.pk), "name": item.name}
                for item in characters
            ],
            "locations": [
                {"id": item.id, "name": item.name}
                for item in locations
            ],
            "visualAssets": [
                {
                    "id": str(item.pk),
                    "title": item.title,
                    "category": item.category,
                    "resolved": True,
                }
                for item in references
            ],
            "unresolvedEntities": [],
        }


def _camera_payload(intent: CameraIntent | None) -> dict[str, Any] | None:
    if intent is None:
        return None
    return {
        "id": str(intent.pk),
        "target": intent.target,
        "azimuth": intent.azimuth,
        "elevation": intent.elevation,
        "distance": intent.distance,
        "framing": intent.framing,
        "lensMm": intent.lens_mm,
        "composition": intent.composition,
        "cameraMetadata": intent.camera_metadata,
        "version": intent.version,
        "updatedAt": intent.updated_at.isoformat(),
    }


def _generation_payload(
    generation: StoryboardKeyframeGeneration | None,
    *,
    request=None,
    outdated: bool = False,
) -> dict[str, Any]:
    if generation is None:
        return {
            "id": None,
            "revision": None,
            "status": "empty",
            "url": None,
            "outdated": False,
            "error": None,
        }
    url = None
    if generation.asset_id:
        url = signed_media_url(
            generation.asset.file.name,
            request,
            project=generation.keyframe.shot.storyboard.scene.project,
        )
    error = None
    if generation.status == "failed":
        error = {
            "code": generation.error_code or "image_generation_failed",
            "message": "Unable to generate storyboard frame.",
        }
    return {
        "id": str(generation.pk),
        "revision": generation.revision,
        "status": generation.status,
        "url": url,
        "outdated": bool(outdated),
        "provider": generation.selected_provider or generation.provider or None,
        "model": generation.selected_model or generation.model or None,
        "error": error,
        "createdAt": generation.created_at.isoformat(),
        "completedAt": (
            generation.completed_at.isoformat()
            if generation.completed_at
            else None
        ),
    }


def _reference_payload(item: KeyframeGenerationReference) -> dict[str, Any]:
    return {
        "id": str(item.pk),
        "referenceType": item.reference_type,
        "sourceKeyframeId": (
            str(item.source_keyframe_id) if item.source_keyframe_id else None
        ),
        "visualReferenceId": (
            str(item.visual_reference_id) if item.visual_reference_id else None
        ),
        "characterId": str(item.character_id) if item.character_id else None,
        "locationId": item.location_id,
        "priority": item.priority,
        "isPrimary": item.is_primary,
        "label": item.label_snapshot,
        "missing": not any(
            (
                item.source_keyframe_id,
                item.visual_reference_id,
                item.character_id,
                item.location_id,
            )
        ),
    }


def keyframe_payload(keyframe: StoryboardKeyframe, *, request=None) -> dict[str, Any]:
    intent = getattr(keyframe, "camera_intent", None)
    revisions = getattr(keyframe, "_storyboard_generation_revisions", None)
    if revisions is None:
        revisions = list(keyframe.generations.all())
    latest_generation = revisions[0] if revisions else None
    active_generation = next(
        (
            item
            for item in revisions
            if item.status in ("queued", "generating")
        ),
        None,
    )
    generation = keyframe.current_generation
    if generation is None:
        generation = next(
            (
                item
                for item in revisions
                if item.status == StoryboardGenerationStatus.READY
            ),
            latest_generation,
        )
    outdated = False
    if generation is not None and generation.status == "ready":
        try:
            # Keep the saved revision immutable while exposing whether the
            # editable storyboard has diverged from its request snapshot.
            from w_craft_back.movie.storyboard.generation import (
                build_generation_snapshot,
            )

            saved_options = generation.request_snapshot.get("generationOptions")
            _, current_fingerprint = build_generation_snapshot(
                keyframe,
                generation_options=(
                    saved_options if isinstance(saved_options, Mapping) else None
                ),
            )
            outdated = current_fingerprint != generation.request_fingerprint
        except StoryboardError:
            outdated = True
    return {
        "id": str(keyframe.pk),
        "type": keyframe.type,
        "position": float(keyframe.position),
        "cameraIntent": _camera_payload(intent),
        "image": _generation_payload(
            generation,
            request=request,
            outdated=outdated,
        ),
        "latestGeneration": _generation_payload(
            latest_generation,
            request=request,
            outdated=(outdated and latest_generation == generation),
        ),
        "activeGeneration": (
            _generation_payload(active_generation, request=request)
            if active_generation
            else None
        ),
        "generationReferences": [
            _reference_payload(item)
            for item in keyframe.generation_references.all()
        ],
        "createdAt": keyframe.created_at.isoformat(),
        "updatedAt": keyframe.updated_at.isoformat(),
    }


def _transition_payload(transition: CameraTransition) -> dict[str, Any]:
    return {
        "id": str(transition.pk),
        "fromKeyframeId": str(transition.from_keyframe_id),
        "toKeyframeId": str(transition.to_keyframe_id),
        "detectedMovement": transition.detected_movement,
        "movementOverride": transition.override_movement,
        "effectiveMovement": (
            transition.override_movement or transition.detected_movement
        ),
        "metadata": transition.metadata,
    }


def shot_payload(shot: StoryboardShot, *, request=None) -> dict[str, Any]:
    keyframes = sorted(shot.keyframes.all(), key=lambda item: item.position)
    readiness = ShotReadinessService.evaluate(shot)
    return {
        "id": str(shot.pk),
        "order": shot.order,
        "title": shot.title,
        "description": shot.description,
        "durationSeconds": (
            float(shot.duration_seconds)
            if shot.duration_seconds is not None
            else None
        ),
        "location": (
            {"id": shot.location_id, "name": shot.location.name}
            if shot.location_id
            else None
        ),
        "characters": [
            {
                "id": str(link.character_id) if link.character_id else None,
                "name": (
                    link.character.name if link.character_id else link.name_snapshot
                ),
                "missing": link.character_id is None,
            }
            for link in shot.character_links.all()
        ],
        "visualReferences": [
            {
                "id": str(link.reference_id) if link.reference_id else None,
                "title": (
                    link.reference.title if link.reference_id else link.title_snapshot
                ),
                "role": link.role,
                "missing": link.reference_id is None,
            }
            for link in shot.visual_references.all()
        ],
        "keyframes": [
            keyframe_payload(item, request=request) for item in keyframes
        ],
        "transitions": [
            _transition_payload(item)
            for item in shot.transitions.all()
        ],
        "readiness": {
            "ready": readiness["ready"],
            "missing": list(readiness["missing"]),
        },
        "version": shot.version,
        "updatedAt": shot.updated_at.isoformat(),
    }


def storyboard_payload(storyboard: SceneStoryboard, *, request=None) -> dict[str, Any]:
    shots = list(storyboard.shots.all())
    status_value = ShotReadinessService.storyboard_status(storyboard)
    ready_count = sum(
        ShotReadinessService.evaluate(shot)["ready"] for shot in shots
    )
    return {
        "id": storyboard.pk,
        "sceneId": storyboard.scene_id,
        "status": status_value,
        "shotsCount": len(shots),
        "readyShotsCount": ready_count,
        "progress": ready_count / len(shots) if shots else 0,
        "sourceSceneVersion": storyboard.source_scene_version,
        "currentSceneVersion": storyboard.scene.version,
        "needsReview": storyboard.needs_review,
        "legacyAssetId": storyboard.asset_id,
        "shots": [shot_payload(item, request=request) for item in shots],
        "updatedAt": storyboard.updated_at.isoformat(),
    }


@transaction.atomic
def initialize_storyboard(
    *,
    actor: Any,
    project_id: int,
    scene_id: int,
    request=None,
) -> tuple[dict[str, Any], bool]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    scene = _scene(project, scene_id, lock=True)
    storyboard = SceneStoryboard.objects.select_for_update().filter(scene=scene).first()
    created = storyboard is None
    if created:
        storyboard = SceneStoryboard.objects.create(
            scene=scene,
            asset=None,
            source_scene_version=scene.version,
            created_by=actor,
            updated_by=actor,
        )
    return storyboard_payload(storyboard, request=request), created


def get_scene_storyboard(
    *,
    actor: Any,
    project_id: int,
    scene_id: int,
    request=None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.VIEW,
    )
    scene = _scene(project, scene_id)
    storyboard = storyboard_queryset().filter(scene=scene).first()
    if storyboard is None:
        raise StoryboardNotFound()
    payload = storyboard_payload(storyboard, request=request)
    payload["context"] = SceneStoryboardContextService.build(scene)
    return payload


def list_scene_storyboards(
    *,
    actor: Any,
    project_id: int,
) -> list[dict[str, Any]]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.VIEW,
    )
    scenes = list(
        Scene.objects.filter(project=project)
        .order_by("order")
        .prefetch_related(
            Prefetch(
                "storyboard__shots",
                queryset=StoryboardShot.objects.prefetch_related(
                    "keyframes__camera_intent",
                    "keyframes__current_generation",
                ),
            )
        )
    )
    result: list[dict[str, Any]] = []
    for scene in scenes:
        storyboard = getattr(scene, "storyboard", None)
        shots = list(storyboard.shots.all()) if storyboard else []
        ready = sum(ShotReadinessService.evaluate(shot)["ready"] for shot in shots)
        result.append(
            {
                "id": scene.id,
                "number": scene.order,
                "title": scene.title,
                "status": (
                    ShotReadinessService.storyboard_status(storyboard)
                    if storyboard
                    else "empty"
                ),
                "shotsCount": len(shots),
                "readyShotsCount": ready,
                "progress": ready / len(shots) if shots else 0,
            }
        )
    return result


def _resolved_shot_relations(
    *,
    project: Project,
    data: Mapping[str, Any],
) -> tuple[Location | None, list[StudioCharacter], list[tuple[ProjectReference, str]]]:
    location = None
    if data.get("locationId") is not None:
        location = Location.objects.filter(
            pk=data["locationId"],
            project=project,
        ).first()
        if location is None:
            raise StoryboardNotFound("Location not found.")
    character_ids = data.get("characterIds", [])
    characters = list(
        StudioCharacter.objects.filter(
            project=project,
            character_id__in=character_ids,
        )
    )
    if len(characters) != len(character_ids):
        raise StoryboardError(
            "A character is missing or belongs to another project.",
            code="STORYBOARD_FOREIGN_CHARACTER",
        )
    visual_items = data.get("visualReferences", [])
    reference_ids = [item["referenceId"] for item in visual_items]
    references = {
        item.pk: item
        for item in ProjectReference.objects.filter(
            project=project,
            id__in=reference_ids,
            archived_at__isnull=True,
        )
    }
    if len(references) != len(set(reference_ids)):
        raise StoryboardError(
            "A visual reference is missing or belongs to another project.",
            code="STORYBOARD_FOREIGN_VISUAL_REFERENCE",
        )
    return (
        location,
        characters,
        [(references[item["referenceId"]], item["role"]) for item in visual_items],
    )


def _replace_shot_relations(
    *,
    shot: StoryboardShot,
    characters: list[StudioCharacter] | None,
    visual_references: list[tuple[ProjectReference, str]] | None,
) -> None:
    if characters is not None:
        shot.character_links.all().delete()
        StoryboardShotCharacter.objects.bulk_create(
            [
                StoryboardShotCharacter(
                    shot=shot,
                    character=item,
                    name_snapshot=item.name,
                )
                for item in characters
            ]
        )
    if visual_references is not None:
        shot.visual_references.all().delete()
        StoryboardShotReference.objects.bulk_create(
            [
                StoryboardShotReference(
                    shot=shot,
                    reference=item,
                    role=role,
                    title_snapshot=item.title,
                )
                for item, role in visual_references
            ]
        )


@transaction.atomic
def create_shot(
    *,
    actor: Any,
    project_id: int,
    storyboard_id: int,
    data: Mapping[str, Any],
    request=None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    storyboard = (
        SceneStoryboard.objects.select_for_update()
        .filter(pk=storyboard_id, scene__project=project)
        .first()
    )
    if storyboard is None:
        raise StoryboardNotFound()
    location, characters, references = _resolved_shot_relations(
        project=project,
        data=data,
    )
    last_order = (
        storyboard.shots.select_for_update().order_by("-order")
        .values_list("order", flat=True).first()
        or 0
    )
    shot = StoryboardShot.objects.create(
        storyboard=storyboard,
        order=last_order + 1,
        title=data.get("title", ""),
        description=data.get("description", ""),
        duration_seconds=data.get("durationSeconds"),
        location=location,
        created_by=actor,
        updated_by=actor,
    )
    _replace_shot_relations(
        shot=shot,
        characters=characters,
        visual_references=references,
    )
    StoryboardKeyframe.objects.create(
        shot=shot,
        type=StoryboardKeyframeType.START,
        position=Decimal("0"),
    )
    StoryboardKeyframe.objects.create(
        shot=shot,
        type=StoryboardKeyframeType.END,
        position=Decimal("1"),
    )
    rebuild_transitions(shot)
    shot = storyboard_queryset().get(pk=storyboard.pk).shots.get(pk=shot.pk)
    return shot_payload(shot, request=request)


def list_shots(
    *,
    actor: Any,
    project_id: int,
    storyboard_id: int,
    request=None,
) -> list[dict[str, Any]]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.VIEW,
    )
    storyboard = storyboard_queryset().filter(
        pk=storyboard_id,
        scene__project=project,
    ).first()
    if storyboard is None:
        raise StoryboardNotFound()
    return [shot_payload(item, request=request) for item in storyboard.shots.all()]


def get_shot(
    *,
    actor: Any,
    project_id: int,
    shot_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.VIEW,
    )
    storyboard = storyboard_queryset().filter(
        scene__project=project,
        shots__pk=shot_id,
    ).first()
    if storyboard is None:
        raise StoryboardNotFound("Shot not found.")
    return shot_payload(storyboard.shots.get(pk=shot_id), request=request)


def get_keyframe(
    *,
    actor: Any,
    project_id: int,
    keyframe_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.VIEW,
    )
    keyframe = StoryboardKeyframe.objects.select_related(
        "camera_intent",
        "current_generation__asset",
        "shot__storyboard__scene__project",
    ).prefetch_related(
        "generation_references__source_keyframe",
        "generation_references__visual_reference",
        "generation_references__character",
        "generation_references__location",
        "generations__asset",
    ).filter(
        pk=keyframe_id,
        shot__storyboard__scene__project=project,
    ).first()
    if keyframe is None:
        raise StoryboardNotFound("Keyframe not found.")
    if keyframe.current_generation is None:
        keyframe._storyboard_generation_revisions = list(
            keyframe.generations.all().order_by("-revision")
        )
    return keyframe_payload(keyframe, request=request)


@transaction.atomic
def duplicate_shot(
    *,
    actor: Any,
    project_id: int,
    shot_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    storyboard, source = _locked_storyboard_shot(project, shot_id)
    new_order = (
        storyboard.shots.order_by("-order").values_list("order", flat=True).first()
        or 0
    ) + 1
    duplicate = StoryboardShot.objects.create(
        storyboard=storyboard,
        order=new_order,
        title=source.title,
        description=source.description,
        duration_seconds=source.duration_seconds,
        location=source.location,
        created_by=actor,
        updated_by=actor,
    )
    StoryboardShotCharacter.objects.bulk_create(
        [
            StoryboardShotCharacter(
                shot=duplicate,
                character=item.character,
                name_snapshot=item.name_snapshot,
            )
            for item in source.character_links.all()
        ]
    )
    StoryboardShotReference.objects.bulk_create(
        [
            StoryboardShotReference(
                shot=duplicate,
                reference=item.reference,
                role=item.role,
                title_snapshot=item.title_snapshot,
            )
            for item in source.visual_references.all()
        ]
    )
    keyframe_map: dict[uuid.UUID, StoryboardKeyframe] = {}
    source_keyframes = list(
        source.keyframes.select_related("camera_intent").order_by("position")
    )
    for old in source_keyframes:
        created = StoryboardKeyframe.objects.create(
            shot=duplicate,
            type=old.type,
            position=old.position,
        )
        keyframe_map[old.pk] = created
        old_camera = getattr(old, "camera_intent", None)
        if old_camera:
            CameraIntent.objects.create(
                keyframe=created,
                target=old_camera.target,
                azimuth=old_camera.azimuth,
                elevation=old_camera.elevation,
                distance=old_camera.distance,
                framing=old_camera.framing,
                lens_mm=old_camera.lens_mm,
                composition=old_camera.composition,
                camera_metadata=old_camera.camera_metadata,
            )
    references: list[KeyframeGenerationReference] = []
    for old in source_keyframes:
        for item in old.generation_references.all():
            references.append(
                KeyframeGenerationReference(
                    keyframe=keyframe_map[old.pk],
                    reference_type=item.reference_type,
                    source_keyframe=(
                        keyframe_map.get(item.source_keyframe_id)
                        or item.source_keyframe
                    ),
                    visual_reference=item.visual_reference,
                    character=item.character,
                    location=item.location,
                    priority=item.priority,
                    is_primary=item.is_primary,
                    label_snapshot=item.label_snapshot,
                )
            )
    KeyframeGenerationReference.objects.bulk_create(references)
    rebuild_transitions(duplicate)
    hydrated = storyboard_queryset().get(pk=storyboard.pk).shots.get(
        pk=duplicate.pk
    )
    return shot_payload(hydrated, request=request)


@transaction.atomic
def delete_shot(
    *,
    actor: Any,
    project_id: int,
    shot_id: uuid.UUID,
) -> None:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    storyboard, shot = _locked_storyboard_shot(project, shot_id)
    _reject_inflight_deletion(
        generations=StoryboardKeyframeGeneration.objects.filter(
            keyframe__shot=shot,
        )
    )
    shot.delete()
    remaining = list(storyboard.shots.order_by("order"))
    offset = len(remaining) + max((item.order for item in remaining), default=0) + 1
    for index, item in enumerate(remaining, start=1):
        item.order = offset + index
        item.save(update_fields=["order", "updated_at"])
    for index, item in enumerate(remaining, start=1):
        item.order = index
        item.version += 1
        item.save(update_fields=["order", "version", "updated_at"])


@transaction.atomic
def update_shot(
    *,
    actor: Any,
    project_id: int,
    shot_id: uuid.UUID,
    data: Mapping[str, Any],
    request=None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    shot = _shot(project, shot_id, lock=True)
    if shot.version != data["expectedVersion"]:
        raise StoryboardConflict(
            "Shot was changed by another request.",
            code="STORYBOARD_VERSION_CONFLICT",
            retryable=True,
        )
    relation_keys = {"locationId", "characterIds", "visualReferences"}
    if relation_keys.intersection(data):
        current = {
            "locationId": (
                data["locationId"] if "locationId" in data else shot.location_id
            ),
            "characterIds": (
                data["characterIds"]
                if "characterIds" in data
                else list(
                    shot.character_links.exclude(character=None).values_list(
                        "character_id", flat=True
                    )
                )
            ),
            "visualReferences": (
                data["visualReferences"]
                if "visualReferences" in data
                else [
                    {"referenceId": link.reference_id, "role": link.role}
                    for link in shot.visual_references.exclude(reference=None)
                ]
            ),
        }
        location, characters, references = _resolved_shot_relations(
            project=project,
            data=current,
        )
        shot.location = location
        _replace_shot_relations(
            shot=shot,
            characters=characters if "characterIds" in data else None,
            visual_references=(
                references if "visualReferences" in data else None
            ),
        )
    for source, target in (
        ("title", "title"),
        ("description", "description"),
        ("durationSeconds", "duration_seconds"),
    ):
        if source in data:
            setattr(shot, target, data[source])
    generation_inputs_changed = bool(
        {"description", "locationId", "characterIds", "visualReferences"}
        .intersection(data)
    )
    if generation_inputs_changed:
        StoryboardKeyframe.objects.filter(shot=shot).update(
            current_generation=None,
        )
    shot.updated_by = actor
    shot.version += 1
    shot.save()
    hydrated = storyboard_queryset().get(pk=shot.storyboard_id).shots.get(pk=shot.pk)
    return shot_payload(hydrated, request=request)


@transaction.atomic
def reorder_shots(
    *,
    actor: Any,
    project_id: int,
    storyboard_id: int,
    shot_ids: list[uuid.UUID],
    request=None,
) -> list[dict[str, Any]]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    storyboard = (
        SceneStoryboard.objects.select_for_update()
        .filter(pk=storyboard_id, scene__project=project)
        .first()
    )
    if storyboard is None:
        raise StoryboardNotFound()
    shots = list(storyboard.shots.select_for_update().order_by("order"))
    if set(shot_ids) != {item.pk for item in shots} or len(shot_ids) != len(shots):
        raise StoryboardError(
            "shotIds must contain every storyboard shot exactly once.",
            code="STORYBOARD_INVALID_REORDER",
        )
    by_id = {item.pk: item for item in shots}
    offset = len(shots) + max((item.order for item in shots), default=0) + 1
    for index, shot_id in enumerate(shot_ids, start=1):
        item = by_id[shot_id]
        item.order = offset + index
        item.save(update_fields=["order", "updated_at"])
    for index, shot_id in enumerate(shot_ids, start=1):
        item = by_id[shot_id]
        item.order = index
        item.version += 1
        item.save(update_fields=["order", "version", "updated_at"])
    hydrated = storyboard_queryset().get(pk=storyboard.pk)
    return [shot_payload(item, request=request) for item in hydrated.shots.all()]


@transaction.atomic
def add_keyframe(
    *,
    actor: Any,
    project_id: int,
    shot_id: uuid.UUID,
    position: Decimal,
    request=None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    shot = _shot(project, shot_id, lock=True)
    list(shot.keyframes.select_for_update())
    try:
        keyframe = StoryboardKeyframe.objects.create(
            shot=shot,
            type=StoryboardKeyframeType.INTERMEDIATE,
            position=position,
        )
    except (IntegrityError, ValidationError) as error:
        raise StoryboardError(
            "Keyframe position is already used or invalid.",
            code="STORYBOARD_INVALID_KEYFRAME_POSITION",
        ) from error
    rebuild_transitions(shot)
    keyframe = StoryboardKeyframe.objects.select_related(
        "camera_intent", "current_generation"
    ).get(pk=keyframe.pk)
    return keyframe_payload(keyframe, request=request)


@transaction.atomic
def update_keyframe(
    *,
    actor: Any,
    project_id: int,
    keyframe_id: uuid.UUID,
    position: Decimal,
    request=None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    _shot, keyframe, _keyframes = _locked_keyframe_set(project, keyframe_id)
    if keyframe.type != StoryboardKeyframeType.INTERMEDIATE:
        raise StoryboardError(
            "Only intermediate keyframes can be moved.",
            code="STORYBOARD_PROTECTED_KEYFRAME",
        )
    keyframe.position = position
    try:
        keyframe.save()
    except (IntegrityError, ValidationError) as error:
        raise StoryboardError(
            "Keyframe position is already used or invalid.",
            code="STORYBOARD_INVALID_KEYFRAME_POSITION",
        ) from error
    rebuild_transitions(keyframe.shot)
    return keyframe_payload(keyframe, request=request)


@transaction.atomic
def delete_keyframe(
    *,
    actor: Any,
    project_id: int,
    keyframe_id: uuid.UUID,
) -> None:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    shot, keyframe, _keyframes = _locked_keyframe_set(project, keyframe_id)
    if keyframe.type != StoryboardKeyframeType.INTERMEDIATE:
        raise StoryboardError(
            "START and END keyframes cannot be deleted.",
            code="STORYBOARD_PROTECTED_KEYFRAME",
        )
    _reject_inflight_deletion(generations=keyframe.generations)
    keyframe.delete()
    rebuild_transitions(shot)


@transaction.atomic
def update_camera_intent(
    *,
    actor: Any,
    project_id: int,
    keyframe_id: uuid.UUID,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    _shot, keyframe, _keyframes = _locked_keyframe_set(project, keyframe_id)
    target = validate_camera_target(project_id=project.id, target=data["target"])
    composition = validate_composition(
        project_id=project.id,
        composition=data.get("composition", []),
    )
    metadata = validate_camera_metadata(
        project_id=project.id,
        framing=data["framing"],
        metadata=data.get("cameraMetadata", {}),
    )
    intent = CameraIntent.objects.select_for_update().filter(keyframe=keyframe).first()
    expected = data.get("expectedVersion")
    if intent and expected is None:
        raise StoryboardError(
            "expectedVersion is required when updating camera intent.",
            code="STORYBOARD_VERSION_REQUIRED",
        )
    if intent and expected is not None and intent.version != expected:
        raise StoryboardConflict(
            "Camera intent was changed by another request.",
            code="STORYBOARD_VERSION_CONFLICT",
            retryable=True,
        )
    values = {
        "target": target,
        "azimuth": data["azimuth"],
        "elevation": data["elevation"],
        "distance": data["distance"],
        "framing": data["framing"],
        "lens_mm": data.get("lensMm"),
        "composition": composition,
        "camera_metadata": metadata,
    }
    if intent is None:
        intent = CameraIntent.objects.create(keyframe=keyframe, **values)
    else:
        for field, value in values.items():
            setattr(intent, field, value)
        intent.version += 1
        intent.save()
    if keyframe.current_generation_id is not None:
        keyframe.current_generation = None
        keyframe.save(update_fields=["current_generation", "updated_at"])
    transitions = recalculate_adjacent_transitions(keyframe)
    previous = transitions["from_previous"]
    following = transitions["to_next"]
    return {
        "cameraIntent": _camera_payload(intent),
        "transitions": {
            "fromPrevious": _transition_payload(previous) if previous else None,
            "toNext": _transition_payload(following) if following else None,
        },
    }


@transaction.atomic
def update_transition(
    *,
    actor: Any,
    project_id: int,
    transition_id: uuid.UUID,
    movement_override: str | None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    transition = CameraTransition.objects.select_for_update().filter(
        pk=transition_id,
        shot__storyboard__scene__project=project,
    ).first()
    if transition is None:
        raise StoryboardNotFound("Transition not found.")
    transition.override_movement = movement_override
    transition.save(update_fields=["override_movement", "updated_at"])
    return _transition_payload(transition)


def suggested_references(
    *,
    actor: Any,
    project_id: int,
    keyframe_id: uuid.UUID,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.VIEW,
    )
    keyframe = _keyframe(project, keyframe_id)
    return {"suggested": ContinuityReferenceService.suggest(keyframe)}


@transaction.atomic
def replace_generation_references(
    *,
    actor: Any,
    project_id: int,
    keyframe_id: uuid.UUID,
    items: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.EDIT_CONTENT,
    )
    keyframe = _keyframe(project, keyframe_id, lock=True)
    replacements: list[KeyframeGenerationReference] = []
    for item in items:
        source = None
        visual = None
        character = None
        location = None
        if item.get("sourceKeyframeId"):
            source = StoryboardKeyframe.objects.filter(
                pk=item["sourceKeyframeId"],
                shot__storyboard__scene__project=project,
            ).first()
            if source is None:
                raise StoryboardNotFound("Reference keyframe not found.")
        if item.get("visualReferenceId"):
            visual = ProjectReference.objects.filter(
                pk=item["visualReferenceId"],
                project=project,
                archived_at__isnull=True,
            ).first()
            if visual is None:
                raise StoryboardNotFound("Visual reference not found.")
        if item.get("characterId"):
            character = StudioCharacter.objects.filter(
                pk=item["characterId"],
                project=project,
            ).first()
            if character is None:
                raise StoryboardNotFound("Character reference not found.")
        if item.get("locationId"):
            location = Location.objects.filter(
                pk=item["locationId"],
                project=project,
            ).first()
            if location is None:
                raise StoryboardNotFound("Location reference not found.")
        reference_type = item["referenceType"]
        source_types = {
            GenerationReferenceType.PREVIOUS_KEYFRAME,
            GenerationReferenceType.PREVIOUS_SHOT,
            GenerationReferenceType.OTHER_STORYBOARD_KEYFRAME,
        }
        target_matches_type = (
            (reference_type in source_types and source is not None)
            or (
                reference_type == GenerationReferenceType.CHARACTER
                and character is not None
            )
            or (
                reference_type == GenerationReferenceType.LOCATION
                and location is not None
            )
            or (
                reference_type
                in (
                    GenerationReferenceType.OBJECT,
                    GenerationReferenceType.CLOTHING,
                )
                and visual is not None
            )
        )
        if not target_matches_type:
            raise StoryboardError(
                "Reference type does not match its target.",
                code="STORYBOARD_INVALID_REFERENCE",
            )
        if source is not None and source.pk == keyframe.pk:
            raise StoryboardError(
                "A keyframe cannot reference itself.",
                code="STORYBOARD_INVALID_REFERENCE",
            )
        label = (
            character.name if character else
            visual.title if visual else
            location.name if location else
            f"Keyframe {source.pk}" if source else ""
        )
        replacements.append(
            KeyframeGenerationReference(
                keyframe=keyframe,
                reference_type=reference_type,
                source_keyframe=source,
                visual_reference=visual,
                character=character,
                location=location,
                priority=item.get("priority", 0),
                is_primary=item.get("isPrimary", False),
                label_snapshot=label,
            )
        )
    keyframe.generation_references.all().delete()
    KeyframeGenerationReference.objects.bulk_create(replacements)
    if keyframe.current_generation_id is not None:
        keyframe.current_generation = None
        keyframe.save(update_fields=["current_generation", "updated_at"])
    return [_reference_payload(item) for item in replacements]


def storyboard_preview(
    *,
    actor: Any,
    project_id: int,
    scene_id: int,
    request=None,
) -> dict[str, Any]:
    payload = get_scene_storyboard(
        actor=actor,
        project_id=project_id,
        scene_id=scene_id,
        request=request,
    )
    return {
        "sceneId": scene_id,
        "shots": [
            {
                "id": shot["id"],
                "duration": shot["durationSeconds"],
                "keyframes": [
                    {
                        "id": keyframe["id"],
                        "position": keyframe["position"],
                        "imageUrl": keyframe["image"]["url"],
                    }
                    for keyframe in shot["keyframes"]
                ],
                "transitions": [
                    {
                        "from": item["fromKeyframeId"],
                        "to": item["toKeyframeId"],
                        "movement": item["effectiveMovement"],
                    }
                    for item in shot["transitions"]
                ],
            }
            for shot in payload["shots"]
        ],
    }
