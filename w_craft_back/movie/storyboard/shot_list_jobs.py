"""Persisted shot-list requests, fenced worker execution, and draft adoption."""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from w_craft_back.movie.project import policy
from w_craft_back.movie.project.dashboard_models import Scene
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.storyboard.editor_drafts import (
    EditorDraftPayloadSerializer,
    save_editor_draft,
)
from w_craft_back.movie.storyboard.errors import (
    StoryboardError,
    StoryboardNotFound,
    validation_error,
)
from w_craft_back.movie.storyboard.models import (
    SceneStoryboardEditorDraft,
    SceneStoryboardShotListJob,
    SceneStoryboardShotListRequest,
    ShotListJobStatus,
    ShotListResultState,
)
from w_craft_back.movie.storyboard.services import (
    SceneStoryboardContextService,
    _require_project,
    _scene,
)
from w_craft_back.movie.storyboard.shot_list import (
    AIShotListService,
    LiteLLMShotListProvider,
)
from w_craft_back.movie.storyboard.source import source_from_scene


logger = logging.getLogger(__name__)
ACTIVE_STATUSES = (ShotListJobStatus.QUEUED, ShotListJobStatus.RUNNING)
MAX_ATTEMPTS = 3


class ShotListLeaseLost(RuntimeError):
    """The current worker must stop without changing another worker's result."""


def job_payload(job: SceneStoryboardShotListJob) -> dict[str, Any]:
    """Expose only scoped status and validated content, never provider internals."""
    return {
        "jobId": str(job.pk),
        "sceneId": job.scene_id,
        "status": job.status,
        "resultState": job.result_state,
        "createdAt": job.created_at.isoformat(),
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "finishedAt": job.finished_at.isoformat() if job.finished_at else None,
        "estimatedSeconds": job.estimated_seconds,
        "expectedRevision": job.expected_revision,
        "model": job.request_snapshot["model"],
        "language": job.request_snapshot["language"],
        "result": job.result,
        "appliedRevision": job.applied_revision,
        "errorCode": job.error_code or None,
    }


@transaction.atomic
def enqueue_shot_list(
    *, actor: Any, project_id: int, scene_id: int, request_id: uuid.UUID,
    model: str | None, max_shots: int, language: str, estimated_seconds: int,
) -> dict[str, Any]:
    """Commit a request before returning; a browser is never its executor."""
    project = _require_project(
        actor=actor, project_id=project_id, action=policy.Action.RUN_GENERATION,
    )
    # Also serialize first requests using the same UUID for different scenes.
    Project.objects.select_for_update().get(pk=project.pk)
    scene = _scene(project, scene_id, lock=True)
    parameters = {
        "sceneId": scene_id, "model": model, "maxShots": max_shots,
        "language": language, "estimatedSeconds": estimated_seconds,
    }
    previous = SceneStoryboardShotListRequest.objects.select_related("job").filter(
        project=project, actor=actor, request_id=request_id,
    ).first()
    if previous:
        if previous.parameters != parameters:
            raise validation_error({"requestId": "Request ID was already used."})
        return job_payload(previous.job)
    job = SceneStoryboardShotListJob.objects.filter(
        scene=scene, status__in=ACTIVE_STATUSES,
    ).first()
    if job is None:
        # Constructor validates allowlist/configuration; it never calls a provider.
        route = LiteLLMShotListProvider(model=model).model
        draft = SceneStoryboardEditorDraft.objects.select_for_update().filter(
            scene=scene,
        ).first()
        job = SceneStoryboardShotListJob.objects.create(
            scene=scene, actor=actor,
            expected_revision=draft.revision if draft else 0,
            estimated_seconds=estimated_seconds,
            request_snapshot={
                "model": route, "language": language, "maxShots": max_shots,
                "context": SceneStoryboardContextService.build(scene),
                "source": source_from_scene(scene),
            },
        )
    SceneStoryboardShotListRequest.objects.create(
        project=project, actor=actor, request_id=request_id,
        parameters=parameters, job=job,
    )
    return job_payload(job)


def list_shot_list_jobs(*, actor: Any, project_id: int) -> dict[str, Any]:
    project = _require_project(
        actor=actor, project_id=project_id, action=policy.Action.VIEW,
    )
    jobs = SceneStoryboardShotListJob.objects.filter(
        scene__project=project,
    ).order_by("scene_id", "-created_at", "-pk").distinct("scene_id")
    return {"jobs": [job_payload(job) for job in jobs]}


def get_shot_list_job(
    *, actor: Any, project_id: int, job_id: uuid.UUID,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor, project_id=project_id, action=policy.Action.VIEW,
    )
    job = SceneStoryboardShotListJob.objects.filter(
        pk=job_id, scene__project=project,
    ).first()
    if job is None:
        raise StoryboardNotFound("Shot-list generation not found.")
    return job_payload(job)


def _locked_scene_job(
    project: Project, job_id: uuid.UUID,
) -> tuple[Scene, SceneStoryboardShotListJob]:
    scene_id = SceneStoryboardShotListJob.objects.filter(
        pk=job_id, scene__project=project,
    ).values_list("scene_id", flat=True).first()
    if scene_id is None:
        raise StoryboardNotFound("Shot-list generation not found.")
    scene = _scene(project, scene_id, lock=True)
    # Keep the same lock order as normal editor saves and worker finalization.
    list(SceneStoryboardEditorDraft.objects.select_for_update().filter(scene=scene))
    job = SceneStoryboardShotListJob.objects.select_for_update().get(pk=job_id)
    return scene, job


def _adopt(
    job: SceneStoryboardShotListJob, *, actor: Any, project_id: int,
    expected_revision: int, mutation_id: uuid.UUID,
) -> None:
    entry = save_editor_draft(
        actor=actor, project_id=project_id, scene_id=job.scene_id,
        expected_revision=expected_revision, mutation_id=mutation_id,
        payload=job.result,
    )
    job.applied_revision = entry["revision"]
    job.apply_mutation_id = mutation_id
    job.result_state = ShotListResultState.APPLIED


@transaction.atomic
def apply_shot_list_job(
    *, actor: Any, project_id: int, job_id: uuid.UUID,
    expected_revision: int, mutation_id: uuid.UUID,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor, project_id=project_id, action=policy.Action.EDIT_CONTENT,
    )
    _, job = _locked_scene_job(project, job_id)
    if job.applied_revision is not None:
        # Replayed adoption must not replace later edits, even with a fresh UUID.
        return job_payload(job)
    if job.status != ShotListJobStatus.SUCCEEDED or job.result is None:
        raise StoryboardError(
            "Shot-list generation is not complete.",
            code="STORYBOARD_SHOT_LIST_NOT_READY", http_status=409,
        )
    if job.result_state == ShotListResultState.DISMISSED:
        raise StoryboardError(
            "This shot-list proposal was dismissed.",
            code="STORYBOARD_SHOT_LIST_DISMISSED", http_status=409,
        )
    _adopt(
        job, actor=actor, project_id=project_id,
        expected_revision=expected_revision, mutation_id=mutation_id,
    )
    job.save(update_fields=[
        "applied_revision", "apply_mutation_id", "result_state", "updated_at",
    ])
    return job_payload(job)


@transaction.atomic
def dismiss_shot_list_job(
    *, actor: Any, project_id: int, job_id: uuid.UUID,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor, project_id=project_id, action=policy.Action.EDIT_CONTENT,
    )
    _, job = _locked_scene_job(project, job_id)
    job.result_state = ShotListResultState.DISMISSED
    job.save(update_fields=["result_state", "updated_at"])
    return job_payload(job)


def shot_list_lease_seconds() -> int:
    """The synchronous provider timeout always fits within its worker fence."""
    return max(
        60, int(getattr(settings, "STORYBOARD_JOB_LEASE_SECONDS", 180)),
        int(getattr(settings, "STORYBOARD_SHOT_LIST_TIMEOUT_SECONDS", 60)) + 60,
    )


@transaction.atomic
def claim_shot_list_job(job_id: uuid.UUID | None = None):
    jobs = SceneStoryboardShotListJob.objects.select_for_update(
        skip_locked=True,
    ).filter(status=ShotListJobStatus.QUEUED, attempts__lt=MAX_ATTEMPTS)
    if job_id is not None:
        jobs = jobs.filter(pk=job_id)
    job = jobs.order_by("created_at").first()
    if job is None:
        return None
    now = timezone.now()
    job.status = ShotListJobStatus.RUNNING
    job.attempts += 1
    job.lease_token = uuid.uuid4()
    job.lease_expires_at = now + timedelta(seconds=shot_list_lease_seconds())
    job.started_at = job.started_at or now
    job.save()
    return job


def _assert_owned(
    job: SceneStoryboardShotListJob, claimed: SceneStoryboardShotListJob,
) -> None:
    if (
        claimed.lease_token is None or job.lease_token != claimed.lease_token
        or job.status != ShotListJobStatus.RUNNING
        or job.lease_expires_at is None or job.lease_expires_at <= timezone.now()
    ):
        raise ShotListLeaseLost()


@transaction.atomic
def _provider_started(claimed: SceneStoryboardShotListJob) -> None:
    job = SceneStoryboardShotListJob.objects.select_for_update().get(pk=claimed.pk)
    _assert_owned(job, claimed)
    job.provider_started_at = timezone.now()
    job.save(update_fields=["provider_started_at", "updated_at"])


def _editor_result(job: SceneStoryboardShotListJob, proposal: dict) -> dict:
    source = proposal["source"]
    document = {
        "sceneId": source["scene_id"], "sceneVersion": source["scene_version"],
        "contentHash": source["content_hash"], "segments": source["segments"],
        "truncated": source["truncated"],
    }
    shots = []
    for index, suggestion in enumerate(proposal["shots"], start=1):
        shot_id = str(uuid.uuid5(job.pk, f"shot-{index}"))
        start_id, end_id = f"{shot_id}-start", f"{shot_id}-end"
        shot = {
            "id": shot_id, "sceneId": str(job.scene_id), "order": index,
            "title": suggestion["title"], "description": suggestion["description"],
            "duration": 4,
            "characterIds": list(dict.fromkeys(suggestion["suggested_characters"])),
            "referenceIds": list(dict.fromkeys(suggestion["suggested_assets"])),
            "keyframes": [{
                "id": frame_id, "shotId": shot_id, "position": position,
                "type": frame_type, "generationStatus": "idle",
                "cameraIntent": {
                    "azimuth": "front", "elevation": "eye-level",
                    "distance": "medium", "lens": 50,
                    "framing": suggestion["suggested_framing"].replace("_", "-"),
                },
            } for frame_id, position, frame_type in (
                (start_id, 0, "start"), (end_id, 1, "end"),
            )],
            "transitions": [{
                "id": f"{shot_id}-transition", "fromKeyframeId": start_id,
                "toKeyframeId": end_id,
            }],
            "source": {
                "document": deepcopy(document), "origin": "ai",
                "segmentIds": suggestion["source_segment_ids"],
            },
        }
        if suggestion["suggested_location"] is not None:
            shot["locationId"] = suggestion["suggested_location"]
        shots.append(shot)
    serializer = EditorDraftPayloadSerializer(data={
        "schemaVersion": 1, "stage": "builder", "shots": shots,
    })
    if not serializer.is_valid():
        raise StoryboardError(
            "The generated shot list cannot be saved as an editor draft.",
            code="STORYBOARD_AI_BAD_RESPONSE", http_status=502,
        )
    return dict(serializer.validated_data)


@transaction.atomic
def _persist_result(
    claimed: SceneStoryboardShotListJob, *, result: dict,
) -> SceneStoryboardShotListJob:
    job = SceneStoryboardShotListJob.objects.select_for_update().get(pk=claimed.pk)
    _assert_owned(job, claimed)
    job.result = result
    job.status = ShotListJobStatus.SUCCEEDED
    job.finished_at = timezone.now()
    job.lease_token = None
    job.lease_expires_at = None
    job.save()
    return job


@transaction.atomic
def _auto_adopt_job(completed: SceneStoryboardShotListJob) -> None:
    project = completed.scene.project
    scene, job = _locked_scene_job(project, completed.pk)
    draft = SceneStoryboardEditorDraft.objects.filter(scene=scene).first()
    revision = draft.revision if draft else 0
    source = job.request_snapshot["source"]
    current_source = source_from_scene(scene)
    if (
        job.result_state == ShotListResultState.PENDING
        and revision == job.expected_revision
        and source["scene_version"] == current_source["scene_version"]
        and source["content_hash"] == current_source["content_hash"]
        and policy.can(job.actor, project, policy.Action.EDIT_CONTENT)
    ):
        _adopt(
            job, actor=job.actor, project_id=project.pk,
            expected_revision=revision, mutation_id=job.pk,
        )
        job.save(update_fields=[
            "applied_revision", "apply_mutation_id", "result_state", "updated_at",
        ])


def finalize_shot_list_job(
    claimed: SceneStoryboardShotListJob, *, result: dict,
) -> SceneStoryboardShotListJob:
    """Commit paid output before attempting optional editor draft adoption."""
    completed = _persist_result(claimed, result=result)
    try:
        _auto_adopt_job(completed)
    except StoryboardError:
        # Permission/source may change during adoption. Keep the successful result.
        pass
    completed.refresh_from_db()
    return completed


@transaction.atomic
def _fail_job(claimed: SceneStoryboardShotListJob, code: str) -> None:
    job = SceneStoryboardShotListJob.objects.select_for_update().filter(
        pk=claimed.pk,
    ).first()
    if job is None:
        return
    try:
        _assert_owned(job, claimed)
    except ShotListLeaseLost:
        return
    job.status = ShotListJobStatus.FAILED
    job.error_code = code[:128]
    job.finished_at = timezone.now()
    job.lease_token = None
    job.lease_expires_at = None
    job.save()


def execute_shot_list_job(job_id: uuid.UUID | None = None):
    """Execute a committed request independently of HTTP/browser connections."""
    claimed = claim_shot_list_job(job_id)
    if claimed is None:
        return None
    try:
        _require_project(
            actor=claimed.actor, project_id=claimed.scene.project_id,
            action=policy.Action.RUN_GENERATION,
        )
        snapshot = claimed.request_snapshot
        service = AIShotListService(model=snapshot["model"])
        _provider_started(claimed)
        proposal = service.suggest(
            context=snapshot["context"], source=snapshot["source"],
            max_shots=snapshot["maxShots"], language=snapshot["language"],
        )
        return finalize_shot_list_job(
            claimed, result=_editor_result(claimed, proposal),
        )
    except (ShotListLeaseLost, StoryboardNotFound):
        return None
    except StoryboardError as error:
        _fail_job(claimed, error.code)
    except Exception as error:
        # Exception text may contain provider response bodies or credentials.
        logger.error(
            "storyboard_shot_list_job_failed",
            extra={"job_id": str(claimed.pk), "exception_type": type(error).__name__},
        )
        _fail_job(claimed, "STORYBOARD_AI_FAILED")
    return SceneStoryboardShotListJob.objects.filter(pk=claimed.pk).first()


@transaction.atomic
def recover_stale_shot_list_jobs(*, limit: int = 100) -> dict[str, list]:
    now = timezone.now()
    jobs = SceneStoryboardShotListJob.objects.select_for_update(
        skip_locked=True,
    ).filter(
        status=ShotListJobStatus.RUNNING, lease_expires_at__lte=now,
    ).order_by("lease_expires_at")[:max(1, min(limit, 1000))]
    result: dict[str, list] = {"requeued": [], "failed": []}
    for job in jobs:
        if job.provider_started_at is not None or job.attempts >= MAX_ATTEMPTS:
            job.status = ShotListJobStatus.FAILED
            job.error_code = (
                "STORYBOARD_AI_OUTCOME_UNKNOWN" if job.provider_started_at
                else "STORYBOARD_AI_MAX_ATTEMPTS"
            )
            job.finished_at = now
            result["failed"].append(job.pk)
        else:
            job.status = ShotListJobStatus.QUEUED
            result["requeued"].append(job.pk)
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
    return result
