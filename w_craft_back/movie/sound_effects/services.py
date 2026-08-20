"""Project policy, orchestration, and public payloads for Sound Effects."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Max, Prefetch
from django.utils import timezone

from w_craft_back.movie.project import policy
from w_craft_back.movie.project.dashboard_models import Scene
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.sound_effects.errors import (
    SoundEffectError,
    public_provider_detail,
)
from w_craft_back.movie.sound_effects.lifecycle import (
    cancel_sound_effect_job,
    enqueue_sound_effect_job,
    retry_sound_effect_job,
)
from w_craft_back.movie.sound_effects.models import (
    SceneSoundEffect,
    SoundEffect,
    SoundEffectGenerationJob,
    SoundEffectJobStatus,
    SoundEffectVariant,
    SoundEffectVersion,
)
from w_craft_back.movie.sound_effects.providers.elevenlabs import (
    MODEL_KEY,
)
from w_craft_back.storage_gateway import signed_url_for_sound_effect_asset


def _project_for_action(
    actor: User,
    project_id: int,
    action: policy.Action,
    *,
    lock: bool = False,
) -> Project:
    queryset = Project.objects.select_for_update() if lock else Project.objects.all()
    project = queryset.filter(pk=project_id).first()
    if project is None or not policy.can(actor, project, policy.Action.VIEW):
        raise SoundEffectError(
            "Project was not found.",
            code="SOUND_EFFECT_PROJECT_NOT_FOUND",
            http_status=404,
        )
    if not policy.can(actor, project, action):
        raise SoundEffectError(
            "You do not have permission for this operation.",
            code="SOUND_EFFECT_PERMISSION_DENIED",
            http_status=403,
        )
    return project


def _permissions(actor: User, project: Project) -> dict[str, Any]:
    summary = policy.permission_summary(actor, project)
    return {
        "currentUserRole": summary["currentUserRole"],
        "canView": summary["canView"],
        "canEdit": summary["canEdit"],
        "canRunGeneration": summary["canRunGeneration"],
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _asset_payload(asset, request) -> dict[str, Any]:
    signed = signed_url_for_sound_effect_asset(asset, request=request)
    expires = (
        timezone.now()
        + timedelta(
            seconds=max(1, int(getattr(settings, "SIGNED_MEDIA_TTL_SECONDS", 300)))
        )
        if signed
        else None
    )
    return {
        "assetId": str(asset.pk),
        "audioUrl": signed,
        "audioUrlExpiresAt": _iso(expires),
        "mimeType": asset.mime_type,
        "durationSeconds": float(asset.duration_seconds),
    }


def _version_payload(version, request) -> dict[str, Any]:
    return {
        "id": str(version.pk),
        "effectId": version.effect_id,
        "versionNumber": version.version_number,
        "asset": _asset_payload(version.asset, request),
        "request": dict(version.request_snapshot or {}),
        "createdAt": _iso(version.created_at),
    }


def _effect_payload(effect: SoundEffect, request) -> dict[str, Any]:
    active = effect.active_version
    return {
        "id": effect.pk,
        "title": effect.title,
        "version": effect.version,
        "activeVersion": _version_payload(active, request) if active else None,
        "archivedAt": _iso(effect.archived_at),
        "createdAt": _iso(effect.created_at),
        "updatedAt": _iso(effect.updated_at),
    }


def get_capabilities(*, actor: User, project_id: int) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    try:
        auto_cost = Decimal(
            str(getattr(settings, "SOUND_EFFECTS_ELEVENLABS_AUTO_COST_USD", ""))
        )
        auto_supported = auto_cost.is_finite() and auto_cost > 0
    except (InvalidOperation, TypeError, ValueError):
        auto_supported = False
    try:
        per_minute = Decimal(
            str(
                getattr(
                    settings,
                    "SOUND_EFFECTS_ELEVENLABS_COST_USD_PER_MINUTE",
                    "",
                )
            )
        )
        price_configured = per_minute.is_finite() and per_minute > 0
    except (InvalidOperation, TypeError, ValueError):
        price_configured = False
    configured = (
        bool(str(getattr(settings, "ELEVENLABS_API_KEY", "") or "").strip())
        and price_configured
        and str(
            getattr(settings, "SOUND_EFFECTS_ELEVENLABS_OUTPUT_FORMAT", "")
        ).strip()
        == "mp3_44100_128"
    )
    return {
        "defaultModelKey": MODEL_KEY,
        "models": [
            {
                "key": MODEL_KEY,
                "label": "ElevenLabs Sound Effects v2",
                "configured": configured,
                "default": True,
                "providerDisplayName": "ElevenLabs",
                "duration": {
                    "autoSupported": auto_supported,
                    "minSeconds": 0.5,
                    "maxSeconds": 30,
                },
                "supportsLoop": True,
                "promptInfluence": {"min": 0, "max": 1, "default": 0.3},
                "outputFormats": ["mp3"],
            }
        ],
        "permissions": _permissions(actor, project),
    }


def list_effects(
    *,
    actor: User,
    project_id: int,
    request,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    effects = SoundEffect.objects.filter(
        project=project,
        archived_at__isnull=True,
    ).select_related("active_version__asset")
    items = [_effect_payload(effect, request) for effect in effects]
    return {
        "items": items,
        "page": {"limit": len(items), "offset": 0, "total": len(items)},
        "permissions": _permissions(actor, project),
    }


def enqueue_job(
    *,
    actor: User,
    project_id: int,
    data: Mapping[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.RUN_GENERATION)
    target_effect = None
    if data.get("targetEffectId") is not None:
        target_effect = SoundEffect.objects.filter(
            pk=data["targetEffectId"],
            project=project,
        ).first()
        if target_effect is None:
            raise SoundEffectError(
                "Target effect was not found.",
                code="SOUND_EFFECT_NOT_FOUND",
                http_status=404,
            )
    target_scene = None
    if data.get("sceneId") is not None:
        target_scene = Scene.objects.filter(
            pk=data["sceneId"],
            project=project,
        ).first()
        if target_scene is None:
            raise SoundEffectError(
                "Target scene was not found.",
                code="SOUND_EFFECT_SCENE_NOT_FOUND",
                http_status=404,
            )
    job, replay = enqueue_sound_effect_job(
        project=project,
        actor=actor,
        request=data,
        idempotency_key=idempotency_key,
        target_effect=target_effect,
        target_scene=target_scene,
    )
    return _accepted_payload(job, replay)


def _accepted_payload(
    job: SoundEffectGenerationJob,
    replay: bool,
) -> dict[str, Any]:
    return {
        "jobId": str(job.pk),
        "modelKey": str(job.provider_snapshot.get("modelKey") or MODEL_KEY),
        "status": job.status,
        "stage": job.stage,
        "idempotentReplay": replay,
        "pollAfterMs": 3000,
        "createdAt": _iso(job.created_at),
    }


def _job_queryset():
    variants = SoundEffectVariant.objects.select_related(
        "asset",
        "applied_version",
    )
    return SoundEffectGenerationJob.objects.select_related(
        "project",
        "target_effect",
        "target_scene",
        "retry_of",
    ).prefetch_related(Prefetch("variant", queryset=variants))


def _job_payload(job: SoundEffectGenerationJob, actor: User, request) -> dict[str, Any]:
    try:
        variant = job.variant
    except SoundEffectVariant.DoesNotExist:
        variant = None
    applied = getattr(variant, "applied_version", None) if variant else None
    variants = []
    if variant is not None:
        variants.append(
            {
                "variantId": str(variant.pk),
                **_asset_payload(variant.asset, request),
                "appliedEffectVersionId": str(applied.pk) if applied else None,
            }
        )
    error = None
    if job.error_code:
        error = {
            "code": job.error_code,
            "detail": (
                public_provider_detail(job.error_code)
                if job.error_code.startswith("SOUND_EFFECT_PROVIDER_")
                else job.error_detail
            ),
            "retryable": job.error_retryable,
        }
    payload = dict(job.request or {})
    payload.update(
        {
            "jobId": str(job.pk),
            "modelKey": str(job.provider_snapshot.get("modelKey") or MODEL_KEY),
            "targetEffectId": job.target_effect_id,
            "sceneId": job.target_scene_id,
            "status": job.status,
            "stage": job.stage,
            "retryOf": str(job.retry_of_id) if job.retry_of_id else None,
            "attempts": job.attempts,
            "canCancel": job.status == SoundEffectJobStatus.QUEUED,
            "canRetry": (
                job.status
                in {SoundEffectJobStatus.FAILED, SoundEffectJobStatus.CANCELLED}
                and job.error_code != "SOUND_EFFECT_PROVIDER_OUTCOME_UNKNOWN"
            ),
            "error": error,
            "variants": variants,
            "createdAt": _iso(job.created_at),
            "completedAt": _iso(job.completed_at),
            "permissions": _permissions(actor, job.project),
        }
    )
    return payload


def list_jobs(*, actor: User, project_id: int, request) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    jobs = _job_queryset().filter(project=project)
    return {
        "items": [_job_payload(job, actor, request) for job in jobs],
        "permissions": _permissions(actor, project),
    }


def _job_for_action(
    *,
    actor: User,
    project_id: int,
    job_id,
    action: policy.Action,
) -> SoundEffectGenerationJob:
    project = _project_for_action(actor, project_id, action)
    job = _job_queryset().filter(pk=job_id, project=project).first()
    if job is None:
        raise SoundEffectError(
            "Sound-effect job was not found.",
            code="SOUND_EFFECT_JOB_NOT_FOUND",
            http_status=404,
        )
    return job


def get_job(*, actor: User, project_id: int, job_id, request) -> dict[str, Any]:
    job = _job_for_action(
        actor=actor,
        project_id=project_id,
        job_id=job_id,
        action=policy.Action.VIEW,
    )
    return _job_payload(job, actor, request)


def cancel_job(*, actor: User, project_id: int, job_id, request) -> dict[str, Any]:
    job = _job_for_action(
        actor=actor,
        project_id=project_id,
        job_id=job_id,
        action=policy.Action.RUN_GENERATION,
    )
    cancel_sound_effect_job(job)
    return get_job(
        actor=actor,
        project_id=project_id,
        job_id=job_id,
        request=request,
    )


def retry_job(*, actor: User, project_id: int, job_id) -> dict[str, Any]:
    job = _job_for_action(
        actor=actor,
        project_id=project_id,
        job_id=job_id,
        action=policy.Action.RUN_GENERATION,
    )
    retry, replay = retry_sound_effect_job(job, actor=actor)
    return _accepted_payload(retry, replay)


@transaction.atomic
def apply_variant(
    *,
    actor: User,
    project_id: int,
    job_id,
    variant_id,
    data: Mapping[str, Any],
    request,
) -> tuple[dict[str, Any], bool]:
    project = _project_for_action(
        actor,
        project_id,
        policy.Action.EDIT_CONTENT,
        lock=True,
    )
    variant = SoundEffectVariant.objects.select_for_update(of=("self",)).select_related(
        "job__target_scene",
        "asset",
    ).filter(pk=variant_id, job_id=job_id, job__project=project).first()
    if variant is None:
        raise SoundEffectError(
            "Sound-effect variant was not found.",
            code="SOUND_EFFECT_VARIANT_NOT_FOUND",
            http_status=404,
        )
    applied = getattr(variant, "applied_version", None)
    if applied is not None:
        return _version_payload(applied, request), False
    target_id = data.get("targetEffectId") or variant.job.target_effect_id
    effect = None
    if target_id is not None:
        effect = SoundEffect.objects.select_for_update().filter(
            pk=target_id,
            project=project,
        ).first()
        if effect is None:
            raise SoundEffectError(
                "Target effect was not found.",
                code="SOUND_EFFECT_NOT_FOUND",
                http_status=404,
            )
        effect.title = data["title"]
    else:
        effect = SoundEffect.objects.create(
            project=project,
            title=data["title"],
            created_by=actor,
        )
    last_number = effect.versions.aggregate(value=Max("version_number"))["value"] or 0
    version = SoundEffectVersion.objects.create(
        effect=effect,
        version_number=last_number + 1,
        asset=variant.asset,
        request_snapshot=dict(variant.job.request or {}),
        source_variant=variant,
        created_by=actor,
    )
    effect.active_version = version
    effect.version += 1
    effect.save(update_fields=("title", "active_version", "version", "updated_at"))
    if variant.job.target_scene_id:
        SceneSoundEffect.objects.update_or_create(
            scene=variant.job.target_scene,
            effect=effect,
            start_time_seconds=0,
            defaults={"effect_version": version},
        )
    return _version_payload(version, request), True


def list_assignments(*, actor: User, project_id: int) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    rows = SceneSoundEffect.objects.filter(
        scene__project=project
    ).select_related("scene", "effect", "effect_version")
    return {
        "items": [
            {
                "id": row.pk,
                "sceneId": row.scene_id,
                "effectId": row.effect_id,
                "effectVersionId": str(row.effect_version_id),
                "startTimeSeconds": float(row.start_time_seconds),
            }
            for row in rows
        ],
        "permissions": _permissions(actor, project),
    }
