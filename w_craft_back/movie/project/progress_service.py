"""Dynamic project-progress calculation for the project dashboard."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional
from uuid import UUID

from django.db.models import Count, Q

from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    StudioCharacter,
)
from w_craft_back.movie.project.dashboard_models import (
    AssetType,
    Scene,
    SceneCharacter,
    SceneStoryboard,
    VideoShot,
)
from w_craft_back.movie.project.models import Project


PROJECT_PROGRESS_WEIGHTS = {
    "script": Fraction(20, 100),
    "characters": Fraction(20, 100),
    "storyboard": Fraction(25, 100),
    "video": Fraction(35, 100),
}

_SPEAKER_RESET_BLOCK_TYPES = {
    "action",
    "camera",
    "note",
    "scene_heading",
    "sound",
    "transition",
}
_EMPTY_SCENE_HEADING_TEMPLATE = "ИНТ. ЛОКАЦИЯ — ДЕНЬ"


@dataclass(frozen=True)
class StoryboardReviewScene:
    scene_id: int
    title: str
    current_revision: int
    accepted_revision: int


@dataclass(frozen=True)
class ProjectProgressSnapshot:
    script: Fraction
    characters: Optional[Fraction]
    storyboard: Fraction
    video: Fraction
    storyboard_review_scenes: tuple[StoryboardReviewScene, ...]
    storyboard_ready_count: int = 0
    video_ready_count: int = 0
    video_total_count: int = 0

    @property
    def overall(self) -> Fraction:
        ratios = {
            "script": self.script,
            "characters": self.characters,
            "storyboard": self.storyboard,
            "video": self.video,
        }
        included = {
            key: ratio for key, ratio in ratios.items() if ratio is not None
        }
        total_weight = sum(
            (PROJECT_PROGRESS_WEIGHTS[key] for key in included),
            start=Fraction(0, 1),
        )
        if not total_weight:
            return Fraction(0, 1)
        weighted = sum(
            (
                PROJECT_PROGRESS_WEIGHTS[key] * ratio
                for key, ratio in included.items()
            ),
            start=Fraction(0, 1),
        )
        return weighted / total_weight

    def as_payload(self) -> dict:
        review_scenes = [
            {
                "sceneId": scene.scene_id,
                "title": scene.title,
                "currentRevision": scene.current_revision,
                "acceptedRevision": scene.accepted_revision,
            }
            for scene in self.storyboard_review_scenes
        ]
        return {
            "overall": float(self.overall),
            "script": float(self.script),
            "characters": (
                float(self.characters) if self.characters is not None else None
            ),
            "storyboard": float(self.storyboard),
            "video": float(self.video),
            "storyboardNeedsReview": len(review_scenes),
            "storyboardReviewScenes": review_scenes,
        }

    def as_percentage_payload(self) -> dict[str, Optional[int]]:
        return {
            "overall": _percent(self.overall),
            "script": _percent(self.script),
            "characters": (
                _percent(self.characters) if self.characters is not None else None
            ),
            "storyboard": _percent(self.storyboard),
            "video": _percent(self.video),
        }


def _percent(value: Fraction) -> int:
    """Round a 0..1 ratio to the nearest integer percent, halves upward."""

    clamped = max(Fraction(0, 1), min(Fraction(1, 1), value))
    return int(clamped * 100 + Fraction(1, 2))


def _scene_has_script_content(scene: Scene) -> bool:
    if (
        (scene.description or "").strip()
        or (scene.notes or "").strip()
    ):
        return True
    blocks = scene.script_blocks if isinstance(scene.script_blocks, list) else []
    if blocks:
        return any(
            isinstance(block, dict)
            and isinstance(block.get("text"), str)
            and block["text"].strip()
            and not (
                block.get("type") == "scene_heading"
                and block["text"].strip().upper()
                == _EMPTY_SCENE_HEADING_TEMPLATE
            )
            for block in blocks
        )
    return bool((scene.script_text or "").strip())


def _uuid(value) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _character_progress(project: Project, scenes: list[Scene]) -> Optional[Fraction]:
    characters = list(
        StudioCharacter.objects.filter(project=project).values(
            "character_id",
            "name",
            "canonical_reference_image_id",
        )
    )
    if not characters:
        return None

    character_ids = {row["character_id"] for row in characters}
    ids_by_name: dict[str, list[UUID]] = defaultdict(list)
    for row in characters:
        normalized_name = (row["name"] or "").strip().casefold()
        if normalized_name:
            ids_by_name[normalized_name].append(row["character_id"])

    appearance_scene_ids: defaultdict[UUID, set[int]] = defaultdict(set)
    for character_id, scene_id in SceneCharacter.objects.filter(
        scene__project=project,
        character_id__in=character_ids,
    ).values_list("character_id", "scene_id"):
        appearance_scene_ids[character_id].add(scene_id)

    replica_counts: Counter[UUID] = Counter()
    for scene in scenes:
        blocks = scene.script_blocks if isinstance(scene.script_blocks, list) else []
        active_speaker_id: Optional[UUID] = None
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            explicit_supplied = "characterId" in block
            explicit_id = _uuid(block.get("characterId"))
            if explicit_id not in character_ids:
                explicit_id = None

            if block_type == "character":
                name_matches = ids_by_name.get(
                    str(block.get("text") or "").strip().casefold(),
                    [],
                )
                active_speaker_id = (
                    explicit_id
                    if explicit_supplied
                    else name_matches[0] if len(name_matches) == 1 else None
                )
                continue

            if block_type in _SPEAKER_RESET_BLOCK_TYPES:
                active_speaker_id = None
                continue

            if explicit_supplied:
                active_speaker_id = explicit_id
            if block_type != "dialogue":
                continue
            if not str(block.get("text") or "").strip():
                continue
            speaker_id = explicit_id if explicit_supplied else active_speaker_id
            if speaker_id is not None:
                replica_counts[speaker_id] += 1
                appearance_scene_ids[speaker_id].add(scene.id)

    appearance_counts = {
        character_id: len(scene_ids)
        for character_id, scene_ids in appearance_scene_ids.items()
    }

    canonical_ids = {
        row["canonical_reference_image_id"]
        for row in characters
        if row["canonical_reference_image_id"] is not None
    }
    ready_character_ids = set(
        CharacterAsset.objects.filter(
            project=project,
            status=CharacterAssetStatus.READY,
        )
        .filter(
            Q(asset_id__in=canonical_ids)
            | Q(
                asset_type__in=(
                    CharacterAssetType.UPLOADED_REFERENCE,
                    CharacterAssetType.PORTRAIT,
                )
            )
        )
        .values_list("character_id", flat=True)
    )

    significant_ids = {
        character_id
        for character_id in character_ids
        if replica_counts[character_id] > 5
        or appearance_counts.get(character_id, 0) >= 2
        or character_id in ready_character_ids
    }
    total_scene_weight = sum(
        appearance_counts.get(character_id, 0)
        for character_id in significant_ids
    )
    if total_scene_weight == 0:
        return None
    ready_scene_weight = sum(
        appearance_counts.get(character_id, 0)
        for character_id in significant_ids
        if character_id in ready_character_ids
    )
    return Fraction(ready_scene_weight, total_scene_weight)


def calculate_project_progress(project: Project) -> ProjectProgressSnapshot:
    scenes = list(
        Scene.objects.filter(project=project).only(
            "id",
            "title",
            "description",
            "script_text",
            "script_blocks",
            "notes",
            "version",
        )
    )
    scene_count = len(scenes)
    script = (
        Fraction(sum(_scene_has_script_content(scene) for scene in scenes), scene_count)
        if scene_count
        else Fraction(0, 1)
    )

    storyboards = {
        storyboard.scene_id: storyboard
        for storyboard in SceneStoryboard.objects.filter(
            scene__project=project,
            asset__asset_type=AssetType.STORYBOARD,
        ).select_related("scene")
    }
    current_storyboards = 0
    review_scenes = []
    for scene in scenes:
        storyboard = storyboards.get(scene.id)
        if storyboard is None:
            continue
        if storyboard.accepted_scene_version == scene.version:
            current_storyboards += 1
        else:
            review_scenes.append(
                StoryboardReviewScene(
                    scene_id=scene.id,
                    title=scene.title,
                    current_revision=scene.version,
                    accepted_revision=storyboard.accepted_scene_version,
                )
            )
    storyboard = (
        Fraction(current_storyboards, scene_count)
        if scene_count
        else Fraction(0, 1)
    )

    shot_counts = VideoShot.objects.filter(project=project).aggregate(
        total=Count("id"),
        ready=Count(
            "id",
            filter=Q(final_asset__asset_type=AssetType.VIDEO),
        ),
    )
    total_shots = shot_counts["total"] or 0
    video = (
        Fraction(shot_counts["ready"] or 0, total_shots)
        if total_shots
        else Fraction(0, 1)
    )

    return ProjectProgressSnapshot(
        script=script,
        characters=_character_progress(project, scenes),
        storyboard=storyboard,
        video=video,
        storyboard_review_scenes=tuple(review_scenes),
        storyboard_ready_count=current_storyboards,
        video_ready_count=shot_counts["ready"] or 0,
        video_total_count=total_shots,
    )
