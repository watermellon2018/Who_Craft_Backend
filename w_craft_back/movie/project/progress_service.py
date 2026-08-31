"""Dynamic project-progress calculation for the project dashboard."""

from __future__ import annotations

import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional
from uuid import UUID

from django.db.models import Count, Prefetch, Q

from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    CharacterRole,
    StudioCharacter,
    VISIBLE_CHARACTER_STATUSES,
)
from w_craft_back.movie.project.dashboard_models import (
    AssetType,
    Scene,
    SceneCharacter,
    SceneStoryboard,
    VideoShot,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.storyboard.domain.readiness import ShotReadinessService
from w_craft_back.movie.storyboard.models import (
    StoryboardKeyframe,
    StoryboardShot,
)


PROJECT_PROGRESS_WEIGHTS = {
    "script": Fraction(20, 100),
    "characters": Fraction(20, 100),
    "storyboard": Fraction(25, 100),
    "video": Fraction(35, 100),
}

MISSING_CHARACTER_MIN_DIALOGUE_COUNT = 6
MISSING_CHARACTER_MIN_SCENE_COUNT = 2
_SPEAKER_RESET_BLOCK_TYPES = frozenset(
    {
        "action",
        "camera",
        "note",
        "scene_heading",
        "sound",
        "transition",
    }
)
_EXCLUDED_MISSING_CHARACTER_ROLES = frozenset(
    {CharacterRole.EPISODIC, CharacterRole.CAMEO}
)
_EMPTY_SCENE_HEADING_TEMPLATE = "ИНТ. ЛОКАЦИЯ — ДЕНЬ"


@dataclass(frozen=True)
class MissingCharacter:
    name: str
    dialogue_count: int
    scene_count: int

    def as_payload(self) -> dict:
        return {
            "name": self.name,
            "dialogueCount": self.dialogue_count,
            "sceneCount": self.scene_count,
        }


@dataclass(frozen=True)
class _MissingSpeaker:
    normalized_name: str
    display_name: str
    has_logical_character: bool = False


@dataclass
class _MissingCharacterMetric:
    name: str
    dialogue_count: int = 0
    scene_ids: set[int] = field(default_factory=set)
    has_logical_character: bool = False


@dataclass(frozen=True)
class VideoPreparationScene:
    scene_id: int
    title: str
    order: int

    def as_payload(self) -> dict:
        return {
            "sceneId": self.scene_id,
            "title": self.title,
            "order": self.order,
        }


@dataclass(frozen=True)
class StoryboardPreparationScene(VideoPreparationScene):
    status: str
    current_version: int
    accepted_version: Optional[int]

    def as_payload(self) -> dict:
        return {
            **super().as_payload(),
            "status": self.status,
            "currentVersion": self.current_version,
            "acceptedVersion": self.accepted_version,
        }


@dataclass(frozen=True)
class VideoPreparationState:
    missing_characters: tuple[MissingCharacter, ...]
    empty_scenes: tuple[VideoPreparationScene, ...]
    storyboard_scenes: tuple[StoryboardPreparationScene, ...]
    storyboard_ready_count: int
    storyboard_total_count: int

    @property
    def storyboard_missing_count(self) -> int:
        return sum(scene.status == "missing" for scene in self.storyboard_scenes)

    @property
    def storyboard_stale_count(self) -> int:
        return sum(scene.status == "stale" for scene in self.storyboard_scenes)

    @property
    def storyboard_ready(self) -> bool:
        return (
            self.storyboard_total_count > 0
            and self.storyboard_ready_count == self.storyboard_total_count
        )

    @property
    def storyboard_progress(self) -> Fraction:
        if not self.storyboard_total_count:
            return Fraction(0, 1)
        return Fraction(
            self.storyboard_ready_count,
            self.storyboard_total_count,
        )

    @property
    def ready(self) -> bool:
        return (
            not self.missing_characters
            and not self.empty_scenes
            and self.storyboard_ready
        )

    @property
    def task_count(self) -> int:
        return (
            len(self.missing_characters)
            + len(self.empty_scenes)
            + (0 if self.storyboard_ready else 1)
        )

    def compact_payload(self) -> dict:
        return {"ready": self.ready, "taskCount": self.task_count}

    def as_payload(self) -> dict:
        return {
            **self.compact_payload(),
            "missingCharacters": [
                character.as_payload() for character in self.missing_characters
            ],
            "emptyScenes": [scene.as_payload() for scene in self.empty_scenes],
            "storyboard": {
                "ready": self.storyboard_ready,
                "progress": float(self.storyboard_progress),
                "readyCount": self.storyboard_ready_count,
                "totalCount": self.storyboard_total_count,
                "missingCount": self.storyboard_missing_count,
                "staleCount": self.storyboard_stale_count,
                "scenes": [scene.as_payload() for scene in self.storyboard_scenes],
            },
        }


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
    video_preparation_ready: bool = False
    video_preparation_task_count: int = 1

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
            "videoPreparation": {
                "ready": self.video_preparation_ready,
                "taskCount": self.video_preparation_task_count,
            },
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


def _clean_character_name(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _normalized_character_name(value) -> str:
    return _clean_character_name(value).casefold()


def _speaker_for_character(
    character: Optional[StudioCharacter],
) -> Optional[_MissingSpeaker]:
    if character is None:
        return None
    if character.role in _EXCLUDED_MISSING_CHARACTER_ROLES:
        return None
    if character.status in VISIBLE_CHARACTER_STATUSES:
        return None
    display_name = _clean_character_name(character.name)
    if not display_name:
        return None
    return _MissingSpeaker(
        normalized_name=display_name.casefold(),
        display_name=display_name,
        has_logical_character=True,
    )


def analyze_missing_characters(
    project: Project,
    scenes: Optional[list[Scene]] = None,
) -> tuple[MissingCharacter, ...]:
    """Return significant screenplay speakers without a visible character."""
    characters = list(StudioCharacter.objects.filter(project=project))
    characters_by_id = {
        str(character.character_id).casefold(): character
        for character in characters
    }
    characters_by_name: dict[str, list[StudioCharacter]] = {}
    for character in characters:
        normalized_name = _normalized_character_name(character.name)
        if normalized_name:
            characters_by_name.setdefault(normalized_name, []).append(character)

    def speaker_for_id(raw_character_id) -> Optional[_MissingSpeaker]:
        if raw_character_id is None:
            return None
        character = characters_by_id.get(str(raw_character_id).casefold())
        return _speaker_for_character(character)

    def speaker_for_name(raw_name) -> Optional[_MissingSpeaker]:
        display_name = _clean_character_name(raw_name)
        if not display_name:
            return None
        normalized_name = display_name.casefold()
        matches = characters_by_name.get(normalized_name, [])
        visible_matches = [
            character
            for character in matches
            if character.status in VISIBLE_CHARACTER_STATUSES
        ]
        if len(visible_matches) == 1:
            return None
        relevant_matches = [
            character
            for character in matches
            if character.role not in _EXCLUDED_MISSING_CHARACTER_ROLES
        ]
        if matches and not relevant_matches:
            return None
        return _MissingSpeaker(
            normalized_name=normalized_name,
            display_name=display_name,
            has_logical_character=bool(relevant_matches),
        )

    metrics: dict[str, _MissingCharacterMetric] = {}
    if scenes is None:
        scenes = list(
            Scene.objects.filter(project=project).only("id", "script_blocks")
        )
    for scene in scenes:
        blocks = scene.script_blocks if isinstance(scene.script_blocks, list) else []
        active_speaker: Optional[_MissingSpeaker] = None
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            has_explicit_character_id = "characterId" in block

            if block_type == "character":
                active_speaker = (
                    speaker_for_id(block.get("characterId"))
                    if has_explicit_character_id
                    else speaker_for_name(block.get("text"))
                )
                if active_speaker is not None:
                    metric = metrics.setdefault(
                        active_speaker.normalized_name,
                        _MissingCharacterMetric(name=active_speaker.display_name),
                    )
                    metric.scene_ids.add(scene.id)
                    metric.has_logical_character |= (
                        active_speaker.has_logical_character
                    )
                continue

            if block_type in _SPEAKER_RESET_BLOCK_TYPES:
                active_speaker = None
                continue

            if has_explicit_character_id:
                active_speaker = speaker_for_id(block.get("characterId"))
                if active_speaker is not None:
                    metric = metrics.setdefault(
                        active_speaker.normalized_name,
                        _MissingCharacterMetric(name=active_speaker.display_name),
                    )
                    metric.scene_ids.add(scene.id)
                    metric.has_logical_character |= (
                        active_speaker.has_logical_character
                    )

            text = block.get("text")
            if (
                block_type != "dialogue"
                or not isinstance(text, str)
                or not text.strip()
                or active_speaker is None
            ):
                continue
            metric = metrics.setdefault(
                active_speaker.normalized_name,
                _MissingCharacterMetric(name=active_speaker.display_name),
            )
            metric.has_logical_character |= active_speaker.has_logical_character
            metric.dialogue_count += 1
            metric.scene_ids.add(scene.id)

    significant = [
        metric
        for metric in metrics.values()
        if metric.dialogue_count >= MISSING_CHARACTER_MIN_DIALOGUE_COUNT
        or len(metric.scene_ids) >= MISSING_CHARACTER_MIN_SCENE_COUNT
        or metric.has_logical_character
    ]
    significant.sort(
        key=lambda metric: (
            -metric.dialogue_count,
            -len(metric.scene_ids),
            _normalized_character_name(metric.name),
        )
    )
    return tuple(
        MissingCharacter(
            name=metric.name,
            dialogue_count=metric.dialogue_count,
            scene_count=len(metric.scene_ids),
        )
        for metric in significant
    )


def calculate_video_preparation(
    project: Project,
    scenes: Optional[list[Scene]] = None,
) -> VideoPreparationState:
    if scenes is None:
        scenes = list(
            Scene.objects.filter(project=project).only(
                "id",
                "title",
                "order",
                "description",
                "script_text",
                "script_blocks",
                "notes",
                "version",
            )
        )

    missing_characters = analyze_missing_characters(project, scenes=scenes)
    empty_scenes = tuple(
        VideoPreparationScene(
            scene_id=scene.id,
            title=scene.title,
            order=scene.order,
        )
        for scene in scenes
        if not _scene_has_script_content(scene)
    )
    storyboard_keyframes = StoryboardKeyframe.objects.select_related(
        "camera_intent",
        "current_generation",
    )
    storyboard_shots = StoryboardShot.objects.prefetch_related(
        Prefetch("keyframes", queryset=storyboard_keyframes)
    )
    storyboards = {
        storyboard.scene_id: storyboard
        for storyboard in SceneStoryboard.objects.filter(
            scene__project=project,
        ).select_related("asset").prefetch_related(
            Prefetch("shots", queryset=storyboard_shots)
        )
    }
    storyboard_ready_count = 0
    storyboard_scenes = []
    for scene in scenes:
        storyboard = storyboards.get(scene.id)
        if storyboard is None:
            storyboard_scenes.append(
                StoryboardPreparationScene(
                    scene_id=scene.id,
                    title=scene.title,
                    order=scene.order,
                    status="missing",
                    current_version=scene.version,
                    accepted_version=None,
                )
            )
            continue
        legacy_ready = (
            storyboard.asset_id is not None
            and storyboard.asset.asset_type == AssetType.STORYBOARD
            and storyboard.accepted_scene_version == scene.version
        )
        structured_shots = list(storyboard.shots.all())
        structured_ready = (
            bool(structured_shots)
            and storyboard.accepted_scene_version == scene.version
            and all(
                ShotReadinessService.evaluate(shot)["ready"]
                for shot in structured_shots
            )
        )
        if legacy_ready or structured_ready:
            storyboard_ready_count += 1
            continue
        status = (
            "stale"
            if storyboard.accepted_scene_version != scene.version
            else "missing"
        )
        storyboard_scenes.append(
            StoryboardPreparationScene(
                scene_id=scene.id,
                title=scene.title,
                order=scene.order,
                status=status,
                current_version=scene.version,
                accepted_version=storyboard.accepted_scene_version,
            )
        )

    return VideoPreparationState(
        missing_characters=missing_characters,
        empty_scenes=empty_scenes,
        storyboard_scenes=tuple(storyboard_scenes),
        storyboard_ready_count=storyboard_ready_count,
        storyboard_total_count=len(scenes),
    )


def video_preparation_payload(project: Project, user) -> dict:
    from w_craft_back.movie.project.policy import permission_summary

    state = calculate_video_preparation(project)
    return {
        "project": {
            "id": project.id,
            "title": project.title,
            "permissions": permission_summary(user, project),
        },
        **state.as_payload(),
    }


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
            "order",
            "description",
            "script_text",
            "script_blocks",
            "notes",
            "version",
        )
    )
    scene_count = len(scenes)
    video_preparation = calculate_video_preparation(project, scenes=scenes)
    script = (
        Fraction(sum(_scene_has_script_content(scene) for scene in scenes), scene_count)
        if scene_count
        else Fraction(0, 1)
    )

    review_scenes = tuple(
        StoryboardReviewScene(
            scene_id=scene.scene_id,
            title=scene.title,
            current_revision=scene.current_version,
            accepted_revision=scene.accepted_version,
        )
        for scene in video_preparation.storyboard_scenes
        if scene.status == "stale" and scene.accepted_version is not None
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
        storyboard=video_preparation.storyboard_progress,
        video=video,
        storyboard_review_scenes=review_scenes,
        storyboard_ready_count=video_preparation.storyboard_ready_count,
        video_ready_count=shot_counts["ready"] or 0,
        video_total_count=total_shots,
        video_preparation_ready=video_preparation.ready,
        video_preparation_task_count=video_preparation.task_count,
    )
