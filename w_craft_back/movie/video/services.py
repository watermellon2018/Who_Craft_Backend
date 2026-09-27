"""Profile-routed preflight, immutable acceptance, and explicit result selection."""
from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy

from django.core import signing
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Max

from w_craft_back.movie.project import policy
from w_craft_back.movie.project.dashboard_models import ProjectAsset, VideoShot
from w_craft_back.movie.project.models import Project
from w_craft_back.personal_api.access import generation_source
from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.services.video_generation.contracts import VideoProviderError
from w_craft_back.storage_gateway import signed_url_for_file

from .errors import VideoError
from .execution import execution_route
from .models import VideoGenerationInputAsset, VideoGenerationJob
from .sources import collect_sources, get_source

PREFLIGHT_SALT = "craft-video-preflight-v1"
PREFLIGHT_SECONDS = 600


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def project_access(actor, project_id: int, action=policy.Action.VIEW) -> Project:
    project = Project.objects.filter(pk=project_id).first()
    if project is None or not policy.can_view(actor, project):
        raise VideoError("VIDEO_PROJECT_NOT_FOUND", "Project not found.", 404)
    if not actor.is_active or not policy.can(actor, project, action):
        raise VideoError("VIDEO_FORBIDDEN", "Permission denied.", 403)
    return project


def _lock_project_access(actor, project_id: int, action) -> Project:
    """Serialize content writes with membership/ownership changes."""
    project = Project.objects.select_for_update().filter(pk=project_id).first()
    if project is None:
        raise VideoError("VIDEO_PROJECT_NOT_FOUND", "Project not found.", 404)
    if (not User.objects.filter(pk=actor.pk, is_active=True).exists()
            or not policy.can(actor, project, action)):
        raise VideoError("VIDEO_FORBIDDEN", "Permission denied.", 403)
    return project


def _permissions(actor, project: Project) -> dict:
    return {"canView": policy.can_view(actor, project),
            "canEdit": policy.can_edit(actor, project),
            "canRunGeneration": policy.can_run_generation(actor, project)}


def stale_reasons(job: VideoGenerationJob, source: dict | None) -> list[str]:
    if source is None:
        return ["source_removed"]
    if source["fingerprint"] == job.source_fingerprint:
        return []
    current = source.get("snapshot", {})
    reasons = [key + "_changed" for key in ("scene", "characters", "references", "shot")
               if current.get(key) != job.source_snapshot.get(key)]
    return reasons or ["source_changed"]


def job_payload(job: VideoGenerationJob, *, source: dict | None = None,
                request=None) -> dict:
    inputs = job.input_snapshot
    route = execution_route(job)
    shot = job.video_shot
    reasons = stale_reasons(job, source)
    return {
        "id": str(job.pk), "videoShotId": shot.pk, "sceneId": shot.scene_id,
        "sourceKind": shot.source_kind, "sourceShotId": shot.source_shot_id,
        "status": job.status, "stage": job.stage,
        "modelId": route.get("modelKey", ""),
        "modelLabel": route.get("modelLabel", ""),
        "generationSource": route.get("generationAccess", {}).get("source"),
        "requestedDurationSeconds": inputs.get("requestedDurationSeconds"),
        "effectiveDurationSeconds": route.get("durationSeconds"),
        "motionPrompt": inputs.get("motionPrompt", ""),
        "aspectRatio": route.get("aspectRatio"), "resolution": route.get("resolution"),
        "sourceFingerprint": job.source_fingerprint,
        "stale": bool(reasons), "staleReasons": reasons,
        "assetId": job.result_asset_id,
        "videoUrl": signed_url_for_file(
            job.result_asset.file, request, project=job.project)
        if job.result_asset_id else None,
        "error": {"code": job.error_code, "detail": job.error_detail,
                  "retryable": job.error_retryable} if job.error_code else None,
        "createdAt": job.created_at.isoformat(),
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
        "pollAfterMs": 3000,
    }


def _source_key(source: dict) -> tuple:
    return source["sceneId"], source["sourceKind"], source["sourceShotId"]


def _public_shot(source: dict, shot: VideoShot | None, jobs: list,
                 request=None) -> dict:
    result = {key: deepcopy(value) for key, value in source.items()
              if key not in ("snapshot", "assetIds", "prompt")}
    for name in ("startFrame", "endFrame"):
        frame = result.get(name)
        if frame:
            asset = ProjectAsset.objects.filter(pk=frame["assetId"]).first()
            frame["imageUrl"] = signed_url_for_file(
                asset.file, request, project=asset.project,
            ) if asset else None
    result.update({
        "videoShotId": shot.pk if shot else None,
        "videoShotVersion": shot.version if shot else None,
        "motionPrompt": shot.motion_prompt if shot else "",
        "selectedJobId": next((str(job.pk) for job in jobs
                               if job.result_asset_id and shot
                               and job.result_asset_id == shot.final_asset_id), None),
        "jobs": [job_payload(job, source=source if not source.get("sourceRemoved")
                             else None, request=request) for job in jobs],
    })
    return result


def workspace(actor, project_id: int, request=None) -> dict:
    project = project_access(actor, project_id)
    scenes = collect_sources(project)
    saved = list(VideoShot.objects.filter(project=project).prefetch_related(
        "generation_jobs__result_asset"))
    mapping = {(shot.scene_id, shot.source_kind, shot.source_shot_id): shot
               for shot in saved if shot.source_kind}
    seen = set()
    for scene in scenes:
        result = []
        for source in scene["shots"]:
            key = _source_key(source)
            seen.add(key)
            shot = mapping.get(key)
            jobs = list(shot.generation_jobs.all()) if shot else []
            result.append(_public_shot(source, shot, jobs, request))
        scene["shots"] = result
    archived = []
    for key, shot in mapping.items():
        if key in seen:
            continue
        jobs = list(shot.generation_jobs.all())
        last = jobs[0] if jobs else None
        source = {"sourceKind": shot.source_kind, "sourceShotId": shot.source_shot_id,
                  "sceneId": shot.scene_id, "title": shot.title, "order": shot.order,
                  "description": "", "durationSeconds": 0, "aspectRatio": "16:9",
                  "startFrame": None, "endFrame": None,
                  "fingerprint": last.source_fingerprint if last else "",
                  "ready": False, "sourceRemoved": True,
                  "blockers": [{"code": "VIDEO_SOURCE_REMOVED",
                                "detail": "The storyboard plan was removed."}]}
        archived.append(_public_shot(source, shot, jobs, request))
    return {"projectId": project.pk, "projectTitle": project.title,
            "permissions": _permissions(actor, project),
            "generationSource": generation_source(actor, "video"),
            "scenes": scenes, "archivedShots": archived}


def _validate_options(payload: dict) -> dict:
    allowed = {"sceneId", "sourceKind", "sourceShotId", "modelId", "motionPrompt",
               "aspectRatio", "resolution"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise VideoError("VIDEO_INVALID_INPUT", "Unknown video parameters.")
    try:
        if isinstance(payload["sceneId"], bool):
            raise ValueError
        scene_id = int(payload["sceneId"])
    except (ValueError, TypeError, KeyError):
        raise VideoError("VIDEO_INVALID_INPUT", "A scene is required.") from None
    kind = payload.get("sourceKind")
    source_id = payload.get("sourceShotId")
    motion = payload.get("motionPrompt", "")
    if (kind not in ("editor_draft", "structured") or not isinstance(source_id, str)
            or not 1 <= len(source_id) <= 256 or not isinstance(motion, str)
            or len(motion) > 6000):
        raise VideoError("VIDEO_INVALID_INPUT", "Invalid plan or motion prompt.")
    for key in ("modelId", "aspectRatio", "resolution"):
        if key in payload and (not isinstance(payload[key], str)
                               or not 1 <= len(payload[key]) <= 200):
            raise VideoError("VIDEO_INVALID_INPUT", "Invalid video model parameters.")
    return {**payload, "sceneId": scene_id, "motionPrompt": motion.strip()}


def options(actor, project_id: int, payload: dict) -> dict:
    from w_craft_back.personal_api.access import resolve_video_access
    from w_craft_back.credits.video import estimate_video_generation
    from w_craft_back.credits.services import GenerationPriceUnavailable
    from w_craft_back.services.video_generation.registry import (
        compatible_model, is_configured, list_models, resolve_model,
    )
    project = project_access(actor, project_id)
    intent = _validate_options(payload)
    source = get_source(project, intent["sceneId"], intent["sourceKind"],
                        intent["sourceShotId"])
    if source is None:
        raise VideoError("VIDEO_SOURCE_NOT_FOUND", "Storyboard plan not found.", 404)
    mode = generation_source(actor, "video")
    if mode == "personal" and "modelId" in intent:
        raise VideoError("VIDEO_PROFILE_ROUTE_REQUIRED",
                         "Choose your video model in profile settings.")
    aspect = intent.get("aspectRatio", source.get("aspectRatio") or "16:9")
    resolution = intent.get("resolution", "720p")
    models = []
    if mode == "platform":
        for model in list_models(include_dynamic=True):
            compatible = compatible_model(
                model, aspect_ratio=aspect, resolution=resolution,
                has_start_frame=bool(source.get("startFrame")),
                has_end_frame=bool(source.get("endFrame")),
            )
            available = compatible and is_configured(model)
            models.append({"id": model.key, "label": model.label,
                           "provider": model.provider, "compatible": available,
                           "unavailableReason": "" if available else (
                               "Conditions are not supported." if not compatible
                               else "Provider is not configured."),
                           "durations": list(model.durations),
                           "aspectRatios": list(model.aspect_ratios),
                           "resolutions": list(model.resolutions)})
    blockers = list(source.get("blockers", []))
    if not policy.can_run_generation(actor, project):
        blockers.append({"code": "VIDEO_FORBIDDEN",
                         "detail": "Generation access required."})
    route = None
    try:
        route = resolve_video_access(
            actor, model_key=intent.get("modelId"), aspect_ratio=aspect,
            resolution=resolution, has_start_frame=bool(source.get("startFrame")),
            has_end_frame=bool(source.get("endFrame")),
            requested_duration=source["durationSeconds"],
        )
        if mode == "platform":
            estimate = estimate_video_generation(
                resolve_model(route["modelKey"]), route["durationSeconds"], resolution)
            route["pricing"] = {"estimatedCost": str(estimate.estimated_cost),
                                "reservationAmount": str(estimate.reservation_amount),
                                "snapshot": estimate.snapshot}
    except GenerationPriceUnavailable:
        blockers.append({"code": "VIDEO_PRICE_UNAVAILABLE",
                         "detail": "Video price is unavailable."})
    except (VideoProviderError, PersonalAPIError) as exc:
        blockers.append({"code": exc.code, "detail": getattr(exc, "message", str(exc))})
    token = None
    if route and not blockers:
        token = signing.dumps({
            "actorId": actor.pk, "projectId": project.pk, "intent": intent,
            "sourceFingerprint": source["fingerprint"], "route": route,
        }, salt=PREFLIGHT_SALT, compress=True)
    duration = route.get("durationSeconds") if route else None
    return {"generationSource": mode, "models": models,
            "selectedModelId": (route.get("modelKey") if route
                                else intent.get("modelId")),
            "requestedDurationSeconds": source["durationSeconds"],
            "effectiveDurationSeconds": duration,
            "durationAdjusted": (duration is not None
                                 and duration != source["durationSeconds"]),
            "canGenerate": bool(token), "blockers": blockers,
            "preflightToken": token, "expiresInSeconds": PREFLIGHT_SECONDS}


def create_job(actor, project_id: int, payload: dict, request=None) -> dict:
    from w_craft_back.personal_api.access import resolve_video_access
    from w_craft_back.credits.video import reserve_video_generation
    project = project_access(actor, project_id, policy.Action.RUN_GENERATION)
    required = {"preflightToken", "clientRequestId"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise VideoError("VIDEO_INVALID_INPUT",
                         "Preflight and request ID are required.")
    try:
        request_id = uuid.UUID(str(payload["clientRequestId"]))
    except (ValueError, TypeError):
        raise VideoError("VIDEO_INVALID_INPUT", "Invalid request ID.") from None
    token = payload["preflightToken"]
    if not isinstance(token, str) or not 1 <= len(token) <= 32768:
        raise VideoError("VIDEO_INVALID_PREFLIGHT", "Invalid preflight.")
    request_hash = fingerprint({"preflightToken": token})
    with transaction.atomic():
        project = _lock_project_access(
            actor, project.pk, policy.Action.RUN_GENERATION,
        )
        existing = VideoGenerationJob.objects.filter(
            project=project, actor=actor, client_request_id=request_id).first()
        if existing:
            if existing.request_fingerprint != request_hash:
                raise VideoError("VIDEO_IDEMPOTENCY_CONFLICT",
                                 "Request ID already used.", 409)
            source = get_source(project, existing.video_shot.scene_id,
                                existing.video_shot.source_kind,
                                existing.video_shot.source_shot_id)
            return {"job": job_payload(existing, source=source, request=request),
                    "idempotentReplay": True}
        try:
            approved = signing.loads(token, salt=PREFLIGHT_SALT,
                                     max_age=PREFLIGHT_SECONDS)
        except (signing.BadSignature, ValueError, TypeError):
            raise VideoError("VIDEO_PREFLIGHT_EXPIRED",
                             "Review the video conditions again.", 409) from None
        if (approved.get("actorId") != actor.pk
                or approved.get("projectId") != project.pk):
            raise VideoError("VIDEO_INVALID_PREFLIGHT", "Invalid preflight.", 409)
        intent = approved["intent"]
        source = get_source(project, intent["sceneId"], intent["sourceKind"],
                            intent["sourceShotId"])
        if (not source or not source["ready"]
                or source["fingerprint"] != approved["sourceFingerprint"]):
            raise VideoError("VIDEO_SOURCE_CHANGED",
                             "Storyboard conditions changed. Review again.", 409)
        route = approved["route"]
        current_route = resolve_video_access(
            actor, model_key=intent.get("modelId"), aspect_ratio=route["aspectRatio"],
            resolution=route["resolution"], has_start_frame=True,
            has_end_frame=bool(source.get("endFrame")),
            requested_duration=source["durationSeconds"],
        )
        routing = {key: value for key, value in route.items() if key != "pricing"}
        if fingerprint(routing) != fingerprint(current_route):
            raise VideoError("VIDEO_ROUTE_CHANGED",
                             "Video API settings changed. Review again.", 409)
        shot = VideoShot.objects.filter(
            scene_id=intent["sceneId"], source_kind=intent["sourceKind"],
            source_shot_id=intent["sourceShotId"]).first()
        if shot and VideoGenerationJob.objects.filter(
            video_shot=shot, status__in=("queued", "processing"),
        ).exists():
            raise VideoError(
                "VIDEO_GENERATION_ACTIVE",
                "This storyboard plan already has an active video generation.", 409,
            )
        if shot is None:
            order = VideoShot.objects.filter(scene_id=intent["sceneId"]).aggregate(
                value=Max("order"))["value"] or 0
            shot = VideoShot.objects.create(
                project=project, scene_id=intent["sceneId"],
                source_kind=intent["sourceKind"], source_shot_id=intent["sourceShotId"],
                order=order + 1, title=source["title"], created_by=actor)
        shot.motion_prompt = intent["motionPrompt"]
        shot.version += 1
        shot.updated_by = actor
        shot.save(update_fields=[
            "motion_prompt", "version", "updated_by", "updated_at"])
        inputs = {"motionPrompt": intent["motionPrompt"],
                  "description": source.get("prompt", source["description"]),
                  "requestedDurationSeconds": source["durationSeconds"],
                  "startAssetId": source["startFrame"]["assetId"],
                  "endAssetId": (source.get("endFrame") or {}).get("assetId")}
        job = VideoGenerationJob.objects.create(
            project=project, video_shot=shot, actor=actor, client_request_id=request_id,
            request_fingerprint=request_hash, source_fingerprint=source["fingerprint"],
            source_snapshot=source["snapshot"], input_snapshot=inputs,
            provider_snapshot=route,
        )
        asset_ids = set(source.get("assetIds", [])) | {inputs["startAssetId"]}
        if inputs["endAssetId"]:
            asset_ids.add(inputs["endAssetId"])
        assets = list(ProjectAsset.objects.filter(pk__in=asset_ids, project=project))
        if len(assets) != len(asset_ids):
            raise VideoError("VIDEO_SOURCE_CHANGED", "Input assets changed.", 409)
        VideoGenerationInputAsset.objects.bulk_create([
            VideoGenerationInputAsset(job=job, asset=asset) for asset in assets
        ])
        reserve_video_generation(job)
    return {"job": job_payload(job, source=source, request=request),
            "idempotentReplay": False}


def list_jobs(actor, project_id: int, scene_id=None, request=None) -> dict:
    project = project_access(actor, project_id)
    jobs = VideoGenerationJob.objects.filter(project=project).select_related(
        "video_shot", "result_asset")
    if scene_id is not None:
        try:
            jobs = jobs.filter(video_shot__scene_id=int(scene_id))
        except (ValueError, TypeError):
            raise VideoError("VIDEO_INVALID_INPUT", "Invalid scene.") from None
    sources = {_source_key(shot): shot for scene in collect_sources(project)
               for shot in scene["shots"]}
    return {"items": [
        job_payload(job, source=sources.get((
            job.video_shot.scene_id, job.video_shot.source_kind,
            job.video_shot.source_shot_id)), request=request)
        for job in jobs[:100]
    ]}


def retrieve_job(actor, project_id: int, job_id, request=None) -> dict:
    project = project_access(actor, project_id)
    job = VideoGenerationJob.objects.select_related(
        "video_shot", "result_asset").filter(pk=job_id, project=project).first()
    if job is None:
        raise VideoError("VIDEO_JOB_NOT_FOUND", "Video job not found.", 404)
    source = get_source(project, job.video_shot.scene_id,
                        job.video_shot.source_kind, job.video_shot.source_shot_id)
    return job_payload(job, source=source, request=request)


def update_shot(
    actor, project_id: int, shot_id: int, payload: dict, *, select=False,
) -> dict:
    project = project_access(actor, project_id, policy.Action.EDIT_CONTENT)
    required = {"expectedVideoShotVersion", "jobId" if select else "motionPrompt"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise VideoError("VIDEO_INVALID_INPUT", "Invalid video shot update.")
    with transaction.atomic():
        project = _lock_project_access(
            actor, project.pk, policy.Action.EDIT_CONTENT,
        )
        shot = VideoShot.objects.select_for_update().filter(
            pk=shot_id, project=project).first()
        if not shot:
            raise VideoError("VIDEO_SHOT_NOT_FOUND", "Video shot not found.", 404)
        if payload["expectedVideoShotVersion"] != shot.version:
            raise VideoError("VIDEO_VERSION_CONFLICT",
                             "Video shot changed. Reload it.", 409)
        if select:
            try:
                job = VideoGenerationJob.objects.filter(
                    pk=uuid.UUID(str(payload["jobId"])), video_shot=shot,
                    status="completed", result_asset__isnull=False).first()
            except (ValueError, TypeError):
                job = None
            if job is None:
                raise VideoError("VIDEO_RESULT_NOT_FOUND",
                                 "Completed video result not found.", 404)
            shot.final_asset_id = job.result_asset_id
        else:
            motion = payload["motionPrompt"]
            if not isinstance(motion, str) or len(motion) > 6000:
                raise VideoError("VIDEO_INVALID_INPUT", "Invalid motion prompt.")
            shot.motion_prompt = motion.strip()
        shot.version += 1
        shot.updated_by = actor
        shot.save()
    return {"videoShotId": shot.pk, "videoShotVersion": shot.version,
            **({"selectedJobId": str(job.pk)} if select
               else {"motionPrompt": shot.motion_prompt})}
