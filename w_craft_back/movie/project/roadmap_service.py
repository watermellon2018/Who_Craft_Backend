"""Project roadmap derived from current production data."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Optional

from django.db.models import F, OuterRef, Q, Subquery

from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    StudioCharacter,
    VISIBLE_CHARACTER_STATUSES,
)
from w_craft_back.movie.music.models import (
    MusicGenerationJob,
    MusicJobStatus,
)
from w_craft_back.movie.project.dashboard_models import MusicTrack
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.progress_service import (
    ProjectProgressSnapshot,
    VideoPreparationState,
    calculate_video_preparation,
)
from w_craft_back.movie.reference_library.models import (
    ProjectReference,
    ReferenceGenerationJob,
    ReferenceJobStatus,
)


_ACTIVE_REFERENCE_JOBS = (
    ReferenceJobStatus.QUEUED,
    ReferenceJobStatus.PROCESSING,
    ReferenceJobStatus.CANCELLATION_REQUESTED,
)
_ACTIVE_MUSIC_JOBS = (
    MusicJobStatus.QUEUED,
    MusicJobStatus.PROCESSING,
    MusicJobStatus.CANCELLATION_REQUESTED,
)
_STEP_ORDER = ("script", "characters", "references", "music", "storyboard", "video")
_REQUIRED_STEP_ORDER = ("script", "characters", "storyboard", "video")
_DEPENDENCY_BLOCKER_CODES = {
    "scriptNotReady",
    "charactersNotReady",
    "storyboardNotReady",
}


@dataclass(frozen=True)
class RoadmapStep:
    key: str
    optional: bool
    state: str
    progress_percent: Optional[int]
    metrics: dict[str, int]
    blockers: tuple[dict[str, Any], ...]
    action_url: str

    def as_payload(self) -> dict[str, Any]:
        """Return the stable, copy-free dashboard contract."""

        return {
            "key": self.key,
            "optional": self.optional,
            "availability": "available",
            "state": self.state,
            "progressPercent": self.progress_percent,
            "metrics": self.metrics,
            "blockers": list(self.blockers),
            "actionUrl": self.action_url,
        }


def _percent(ready: int, total: int) -> Optional[int]:
    if total <= 0:
        return None
    return min(100, max(0, (ready * 100 + total // 2) // total))


def _fraction_percent(value: Optional[Fraction]) -> Optional[int]:
    if value is None:
        return None
    return _percent(value.numerator, value.denominator)


def _blocker(code: str, count: Optional[int] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code}
    if count is not None:
        payload["count"] = count
    return payload


def _script_step(
    project: Project,
    snapshot: ProjectProgressSnapshot,
    preparation: VideoPreparationState,
) -> RoadmapStep:
    scenes_total = preparation.storyboard_total_count
    scenes_ready = scenes_total - len(preparation.empty_scenes)
    if scenes_total == 0:
        state = "not_started"
    elif scenes_ready == scenes_total:
        state = "ready"
    else:
        state = "in_progress"
    blockers = (
        (_blocker("incompleteScenes", scenes_total - scenes_ready),)
        if scenes_total > scenes_ready
        else ()
    )
    return RoadmapStep(
        key="script",
        optional=False,
        state=state,
        progress_percent=_fraction_percent(snapshot.script),
        metrics={"scenesTotal": scenes_total, "scenesReady": scenes_ready},
        blockers=blockers,
        action_url=f"/project/{project.id}/script",
    )


def _character_step(
    project: Project,
    preparation: VideoPreparationState,
    script_ready: bool,
) -> RoadmapStep:
    characters_total = StudioCharacter.objects.filter(project=project).count()
    characters_ready = (
        CharacterAsset.objects.filter(
            project=project,
            status=CharacterAssetStatus.READY,
            character__status__in=VISIBLE_CHARACTER_STATUSES,
        )
        .filter(
            Q(character__canonical_reference_image_id=F("asset_id"))
            | Q(is_canonical=True)
            | Q(
                asset_type__in=(
                    CharacterAssetType.UPLOADED_REFERENCE,
                    CharacterAssetType.PORTRAIT,
                )
            )
        )
        .values("character_id")
        .distinct()
        .count()
    )
    missing_count = len(preparation.missing_characters)
    missing_without_character = sum(
        not character.has_logical_character
        for character in preparation.missing_characters
    )
    characters_planned = characters_total + missing_without_character

    if script_ready and missing_count:
        state = "needs_attention"
    elif characters_planned == 0:
        state = "ready" if script_ready else "not_started"
    elif characters_ready == characters_planned:
        state = "ready"
    else:
        state = "in_progress"

    progress_percent = _percent(characters_ready, characters_planned)

    blockers = (
        (_blocker("missingCharacters", missing_count),)
        if missing_count and script_ready
        else ()
    )
    return RoadmapStep(
        key="characters",
        optional=False,
        state=state,
        progress_percent=progress_percent,
        metrics={
            "charactersTotal": characters_planned,
            "charactersReady": characters_ready,
        },
        blockers=blockers,
        action_url=f"/project/{project.id}/characters",
    )


def _reference_step(project: Project) -> RoadmapStep:
    latest_job_status = ReferenceGenerationJob.objects.filter(
        reference_id=OuterRef("pk")
    ).order_by("-created_at").values("status")[:1]
    references = list(
        ProjectReference.objects.filter(
            project=project,
            archived_at__isnull=True,
        )
        .annotate(latest_job_status=Subquery(latest_job_status))
        .values("active_version_id", "latest_job_status")
    )
    references_total = len(references)
    references_ready = sum(
        row["active_version_id"] is not None for row in references
    )
    active_jobs = ReferenceGenerationJob.objects.filter(
        project=project,
        reference__archived_at__isnull=True,
        status__in=_ACTIVE_REFERENCE_JOBS,
    ).count()
    failed_references = sum(
        row["active_version_id"] is None
        and row["latest_job_status"] == ReferenceJobStatus.FAILED
        for row in references
    )

    if references_total == 0:
        state = "not_started"
    elif failed_references:
        state = "needs_attention"
    elif active_jobs or references_ready < references_total:
        state = "in_progress"
    else:
        state = "ready"
    blockers = (
        (_blocker("generationFailed", failed_references),)
        if failed_references
        else ()
    )
    return RoadmapStep(
        key="references",
        optional=True,
        state=state,
        progress_percent=_percent(references_ready, references_total),
        metrics={
            "referencesTotal": references_total,
            "referencesReady": references_ready,
            "activeJobs": active_jobs,
            "failedReferences": failed_references,
        },
        blockers=blockers,
        action_url=f"/project/{project.id}/references",
    )


def _music_step(project: Project) -> RoadmapStep:
    latest_job_status = MusicGenerationJob.objects.filter(
        target_track_id=OuterRef("pk")
    ).order_by("-created_at").values("status")[:1]
    tracks = list(
        MusicTrack.objects.filter(
            project=project,
            archived_at__isnull=True,
        )
        .annotate(latest_job_status=Subquery(latest_job_status))
        .values("active_version_id", "latest_job_status")
    )
    tracks_total = len(tracks)
    tracks_ready = sum(row["active_version_id"] is not None for row in tracks)
    relevant_jobs = MusicGenerationJob.objects.filter(project=project).filter(
        Q(target_track__isnull=True) | Q(target_track__archived_at__isnull=True)
    )
    active_jobs = relevant_jobs.filter(status__in=_ACTIVE_MUSIC_JOBS).count()
    failed_tracks = sum(
        row["active_version_id"] is None
        and row["latest_job_status"] == MusicJobStatus.FAILED
        for row in tracks
    )
    latest_unassigned_status = (
        relevant_jobs.filter(target_track__isnull=True)
        .order_by("-created_at")
        .values_list("status", flat=True)
        .first()
    )
    if latest_unassigned_status == MusicJobStatus.FAILED:
        failed_tracks += 1

    if tracks_total == 0 and not active_jobs and latest_unassigned_status is None:
        state = "not_started"
    elif failed_tracks:
        state = "needs_attention"
    elif active_jobs or tracks_ready < tracks_total or tracks_total == 0:
        state = "in_progress"
    else:
        state = "ready"
    blockers = (
        (_blocker("generationFailed", failed_tracks),)
        if failed_tracks
        else ()
    )
    return RoadmapStep(
        key="music",
        optional=True,
        state=state,
        progress_percent=_percent(tracks_ready, tracks_total),
        metrics={
            "tracksTotal": tracks_total,
            "tracksReady": tracks_ready,
            "activeJobs": active_jobs,
            "failedTracks": failed_tracks,
        },
        blockers=blockers,
        action_url=f"/project/{project.id}/music",
    )


def _prerequisite_blockers(
    script_ready: bool,
    characters_ready: bool,
) -> list[dict[str, Any]]:
    blockers = []
    if not script_ready:
        blockers.append(_blocker("scriptNotReady"))
    if not characters_ready:
        blockers.append(_blocker("charactersNotReady"))
    return blockers


def _storyboard_step(
    project: Project,
    snapshot: ProjectProgressSnapshot,
    preparation: VideoPreparationState,
) -> RoadmapStep:
    scenes_total = preparation.storyboard_total_count
    scenes_ready = preparation.storyboard_ready_count
    scenes_stale = preparation.storyboard_stale_count
    blockers = []
    if scenes_stale:
        blockers.append(_blocker("staleStoryboards", scenes_stale))

    if scenes_total > 0 and scenes_ready == scenes_total:
        state = "ready"
    elif preparation.storyboard_started_count:
        state = "in_progress"
    else:
        state = "not_started"

    return RoadmapStep(
        key="storyboard",
        optional=False,
        state=state,
        progress_percent=_fraction_percent(snapshot.storyboard),
        metrics={
            "scenesTotal": scenes_total,
            "scenesReady": scenes_ready,
            "scenesStarted": preparation.storyboard_started_count,
            "scenesMissing": preparation.storyboard_missing_count,
            "scenesStale": scenes_stale,
        },
        blockers=tuple(blockers),
        action_url=f"/project/{project.id}/storyboard",
    )


def _video_step(
    project: Project,
    snapshot: ProjectProgressSnapshot,
    script_ready: bool,
    characters_ready: bool,
    storyboard_ready: bool,
) -> RoadmapStep:
    shots_total = snapshot.video_total_count
    shots_ready = snapshot.video_ready_count
    blockers = _prerequisite_blockers(script_ready, characters_ready)
    if not storyboard_ready:
        blockers.append(_blocker("storyboardNotReady"))
    prerequisites_ready = script_ready and characters_ready and storyboard_ready

    if not prerequisites_ready:
        state = "needs_attention" if shots_total else "blocked"
    elif shots_total > 0 and shots_ready == shots_total:
        state = "ready"
    elif shots_total:
        state = "in_progress"
    else:
        state = "not_started"

    return RoadmapStep(
        key="video",
        optional=False,
        state=state,
        progress_percent=_fraction_percent(snapshot.video),
        metrics={"shotsTotal": shots_total, "shotsReady": shots_ready},
        blockers=tuple(blockers),
        action_url=f"/project/{project.id}/video",
    )


def build_project_roadmap(
    project: Project,
    progress_snapshot: ProjectProgressSnapshot,
) -> dict[str, Any]:
    """Build roadmap v1 without persisting a mutable current stage."""

    preparation = progress_snapshot.video_preparation
    if preparation is None:
        preparation = calculate_video_preparation(project)

    script = _script_step(project, progress_snapshot, preparation)
    characters = _character_step(
        project,
        preparation,
        script_ready=script.state == "ready",
    )
    references = _reference_step(project)
    music = _music_step(project)
    storyboard = _storyboard_step(
        project,
        progress_snapshot,
        preparation,
    )
    video = _video_step(
        project,
        progress_snapshot,
        script_ready=script.state == "ready",
        characters_ready=characters.state == "ready",
        storyboard_ready=storyboard.state == "ready",
    )
    steps_by_key = {
        step.key: step
        for step in (script, characters, references, music, storyboard, video)
    }
    steps = [steps_by_key[key] for key in _STEP_ORDER]

    next_step = next(
        (
            steps_by_key[key]
            for state in ("needs_attention", "in_progress", "not_started")
            for key in _REQUIRED_STEP_ORDER
            if steps_by_key[key].state == state
            and not any(
                blocker["code"] in _DEPENDENCY_BLOCKER_CODES
                for blocker in steps_by_key[key].blockers
            )
        ),
        None,
    )
    next_action = (
        {"stepKey": next_step.key, "actionUrl": next_step.action_url}
        if next_step is not None
        else None
    )
    return {
        "version": 1,
        "steps": [step.as_payload() for step in steps],
        "nextAction": next_action,
    }
