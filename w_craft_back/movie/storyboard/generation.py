"""Build immutable Storyboard image requests and enqueue durable revisions."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from django.db import IntegrityError, transaction

from w_craft_back.credits.pricing import estimate_for_pinned_provider
from w_craft_back.credits.services import (
    CreditServiceError,
    generation_charge_payload,
    reserve_generation,
)
from w_craft_back.movie.project import policy
from w_craft_back.movie.reference_library.errors import (
    ReferenceError as ReferenceLibraryError,
    map_provider_error,
)
from w_craft_back.movie.reference_library.providers import (
    effective_reference_model_key,
    resolve_reference_provider,
)
from w_craft_back.movie.storyboard.errors import (
    StoryboardConflict,
    StoryboardError,
    StoryboardNotFound,
)
from w_craft_back.movie.storyboard.models import (
    StoryboardGenerationStatus,
    StoryboardKeyframe,
    StoryboardKeyframeGeneration,
)
from w_craft_back.movie.storyboard.prompt_compiler import (
    StoryboardGenerationRequest,
    compile_storyboard_prompt,
)
from w_craft_back.movie.storyboard.services import (
    SceneStoryboardContextService,
    _locked_keyframe_set,
    _require_project,
)
from w_craft_back.services.image_generation.errors import ImageProviderError
from w_craft_back.storage_gateway import signed_media_url


ACTIVE_STATUSES = (
    StoryboardGenerationStatus.QUEUED,
    StoryboardGenerationStatus.GENERATING,
)


def _fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reference_snapshot(item) -> dict[str, Any]:
    storage_key = ""
    mime_type = "image/png"
    source_id = None
    if item.source_keyframe_id:
        source_id = str(item.source_keyframe_id)
        generation = item.source_keyframe.current_generation
        if generation and generation.asset_id:
            storage_key = generation.asset.file.name
            mime_type = generation.asset.metadata.get("mime_type", mime_type)
    elif item.visual_reference_id:
        source_id = str(item.visual_reference_id)
        version = item.visual_reference.active_version
        if version and version.asset_id:
            storage_key = version.asset.file.name
            mime_type = version.asset.metadata.get("mime_type", mime_type)
    elif item.character_id:
        source_id = str(item.character_id)
        asset = item.character.canonical_reference_image
        if asset:
            storage_key = asset.storage_path
            mime_type = asset.mime_type or mime_type
    elif item.location_id:
        source_id = str(item.location_id)
        if item.location.image:
            storage_key = item.location.image.name
    return {
        "type": item.reference_type,
        "id": source_id,
        "label": item.label_snapshot,
        "priority": item.priority,
        "isPrimary": item.is_primary,
        "storageKey": storage_key,
        "mimeType": mime_type,
        "missing": not bool(storage_key),
    }


def build_generation_snapshot(
    keyframe: StoryboardKeyframe,
    *,
    generation_options: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    shot = keyframe.shot
    scene = shot.storyboard.scene
    project = scene.project
    camera = getattr(keyframe, "camera_intent", None)
    if camera is None:
        raise StoryboardError(
            "Camera intent is required before generation.",
            code="STORYBOARD_CAMERA_REQUIRED",
        )
    character_links = list(shot.character_links.select_related("character"))
    visual_links = list(
        shot.visual_references.select_related(
            "reference",
            "reference__active_version",
            "reference__active_version__asset",
        )
    )
    references = [
        _reference_snapshot(item)
        for item in keyframe.generation_references.select_related(
            "source_keyframe__current_generation__asset",
            "visual_reference__active_version__asset",
            "character__canonical_reference_image",
            "location",
        ).order_by("-is_primary", "priority", "created_at")
    ]
    primary = next((item for item in references if item["isPrimary"]), None)
    if primary and primary["missing"]:
        raise StoryboardError(
            "The primary generation reference is missing an image.",
            code="STORYBOARD_PRIMARY_REFERENCE_MISSING",
        )
    available = [item for item in references if not item["missing"]]
    if primary is None and available:
        primary = available[0]
    additional = [item for item in available if item is not primary]
    camera_snapshot = {
        "target": camera.target,
        "azimuth": camera.azimuth,
        "elevation": camera.elevation,
        "distance": camera.distance,
        "framing": camera.framing,
        "lens_mm": camera.lens_mm,
        "camera_metadata": camera.camera_metadata,
    }
    generation_request = StoryboardGenerationRequest(
        scene_text=SceneStoryboardContextService.scene_text(scene),
        shot_description=shot.description,
        location=(
            {"id": shot.location_id, "name": shot.location.name}
            if shot.location_id
            else None
        ),
        characters=[
            {
                "id": str(item.character_id) if item.character_id else None,
                "name": (
                    item.character.name if item.character_id else item.name_snapshot
                ),
                "missing": item.character_id is None,
            }
            for item in character_links
        ],
        visual_assets=[
            {
                "id": str(item.reference_id) if item.reference_id else None,
                "title": (
                    item.reference.title if item.reference_id else item.title_snapshot
                ),
                "role": item.role,
                "missing": item.reference_id is None,
            }
            for item in visual_links
        ],
        camera_intent=camera_snapshot,
        composition=list(camera.composition),
        primary_reference=primary,
        additional_references=additional,
        style_reference=None,
    )
    snapshot = generation_request.snapshot()
    snapshot.update(
        {
            "schemaVersion": 1,
            "projectId": project.id,
            "sceneId": scene.id,
            "sceneVersion": scene.version,
            "storyboardId": shot.storyboard_id,
            "shotId": str(shot.pk),
            "keyframeId": str(keyframe.pk),
            "projectGenerationSettings": dict(project.generation_settings or {}),
        }
    )
    if generation_options is not None:
        snapshot["generationOptions"] = dict(generation_options)
    snapshot["compiledPrompt"] = compile_storyboard_prompt(generation_request)
    return snapshot, _fingerprint(snapshot)


def generation_payload(generation: StoryboardKeyframeGeneration, *, request=None):
    project = generation.keyframe.shot.storyboard.scene.project
    image_url = None
    if generation.asset_id:
        image_url = signed_media_url(
            generation.asset.file.name,
            request,
            project=project,
        )
    error = None
    if generation.status == StoryboardGenerationStatus.FAILED:
        error = {
            "code": generation.error_code or "image_generation_failed",
            "message": "Unable to generate storyboard frame.",
        }
    return {
        "generationId": str(generation.pk),
        "keyframeId": str(generation.keyframe_id),
        "revision": generation.revision,
        "status": generation.status,
        "imageUrl": image_url,
        "provider": generation.selected_provider or generation.provider or None,
        "model": generation.selected_model or generation.model or None,
        "error": error,
        "createdAt": generation.created_at.isoformat(),
        "startedAt": (
            generation.started_at.isoformat() if generation.started_at else None
        ),
        "completedAt": (
            generation.completed_at.isoformat()
            if generation.completed_at
            else None
        ),
        "billing": generation_charge_payload("storyboard", str(generation.pk)),
    }


@transaction.atomic
def enqueue_generation(
    *,
    actor: Any,
    project_id: int,
    keyframe_id: uuid.UUID,
    data: Mapping[str, Any],
    idempotency_key: str,
    request=None,
) -> tuple[dict[str, Any], bool]:
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or any(ord(character) < 32 for character in idempotency_key)
    ):
        raise StoryboardError(
            "A valid Idempotency-Key header is required.",
            code="STORYBOARD_IDEMPOTENCY_REQUIRED",
        )
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.RUN_GENERATION,
    )
    _shot, keyframe, _keyframes = _locked_keyframe_set(project, keyframe_id)
    generation_options = {
        "imageModel": str(data.get("imageModel") or ""),
        "routingMode": str(data.get("routingMode") or "manual").lower(),
    }
    snapshot, fingerprint = build_generation_snapshot(
        keyframe,
        generation_options=generation_options,
    )
    existing = keyframe.generations.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise StoryboardConflict(
                "Idempotency key was used for a different generation input.",
                code="STORYBOARD_IDEMPOTENCY_MISMATCH",
            )
        return generation_payload(existing, request=request), False
    active = keyframe.generations.filter(status__in=ACTIVE_STATUSES).first()
    if active:
        raise StoryboardConflict(
            "A generation is already active for this keyframe.",
            code="STORYBOARD_GENERATION_ACTIVE",
            retryable=True,
        )

    try:
        effective_model = effective_reference_model_key(
            actor=actor,
            project=project,
            requested_model=str(data.get("imageModel") or ""),
        )
        routing_mode = generation_options["routingMode"]
        prompt = str(snapshot["compiledPrompt"])
        require_reference = bool(snapshot.get("primary_reference"))
        if routing_mode != "manual":
            from w_craft_back.services.image_generation.routing import (
                build_routing_decision,
            )

            decision = build_routing_decision(
                mode=routing_mode,
                requested_model=effective_model,
                operation="reference" if require_reference else "generate",
                variant_count=1,
                prompt=prompt,
                resolution="1K",
            )
            provider_snapshot = decision.snapshot()
            provider_name = decision.primary.spec.backend
            model_name = decision.primary.spec.model_id
            effective_model = decision.primary.spec.key
        else:
            provider = resolve_reference_provider(
                actor=actor,
                project=project,
                requested_model=effective_model,
                require_edit=False,
            )
            if require_reference and hasattr(provider, "supports_reference"):
                if not provider.supports_reference():
                    raise StoryboardError(
                        "Selected image model does not support references.",
                        code="STORYBOARD_REFERENCE_NOT_SUPPORTED",
                    )
            provider_snapshot = (
                {"spec": provider.spec.__dict__}
                if getattr(provider, "spec", None) is not None
                else {}
            )
            provider_name = provider.name
            model_name = provider.model_id
    except ImageProviderError as error:
        mapped = map_provider_error(error)
        raise StoryboardError(
            mapped.detail,
            code=mapped.code,
            http_status=mapped.http_status,
            retryable=mapped.retryable,
        ) from error
    except ReferenceLibraryError as error:
        raise StoryboardError(
            error.detail,
            code=error.code,
            http_status=error.http_status,
            retryable=error.retryable,
        ) from error

    revision = (
        keyframe.generations.order_by("-revision")
        .values_list("revision", flat=True).first()
        or 0
    ) + 1
    try:
        generation = StoryboardKeyframeGeneration.objects.create(
            keyframe=keyframe,
            revision=revision,
            actor=actor,
            request_snapshot=snapshot,
            request_fingerprint=fingerprint,
            requested_model=effective_model,
            provider_snapshot=provider_snapshot,
            provider=provider_name,
            model=model_name,
            idempotency_key=idempotency_key,
        )
        if provider_snapshot.get("candidates"):
            from w_craft_back.services.image_generation.routing import (
                estimate_route_snapshot,
            )

            estimate, reservation, pricing = estimate_route_snapshot(
                provider_snapshot,
                operation="reference" if require_reference else "generate",
                variant_count=1,
                prompt=prompt,
                resolution="1K",
            )
        else:
            estimate = estimate_for_pinned_provider(
                provider=provider_name,
                provider_snapshot=provider_snapshot or None,
                model_name=model_name,
                operation="reference" if require_reference else "generate",
                variant_count=1,
                prompt=prompt,
                resolution="1K",
            )
            reservation = estimate.reservation_amount
            pricing = estimate.snapshot
        reserve_generation(
            user=actor,
            domain="storyboard",
            job_id=str(generation.pk),
            provider=estimate.provider,
            model_name=estimate.model_name,
            estimated_cost=estimate.estimated_cost,
            reservation_amount=reservation,
            pricing_snapshot=pricing,
            project=project,
            operation="generate",
            routing_mode=routing_mode,
        )
    except CreditServiceError as error:
        raise StoryboardError(
            error.message,
            code=error.code,
            http_status=error.http_status,
        ) from error
    except IntegrityError as error:
        raise StoryboardConflict(
            "A generation is already active for this keyframe.",
            code="STORYBOARD_GENERATION_ACTIVE",
            retryable=True,
        ) from error
    return generation_payload(generation, request=request), True


def get_generation(
    *,
    actor: Any,
    project_id: int,
    generation_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _require_project(
        actor=actor,
        project_id=project_id,
        action=policy.Action.VIEW,
    )
    generation = StoryboardKeyframeGeneration.objects.select_related(
        "asset",
        "keyframe__shot__storyboard__scene__project",
    ).filter(
        pk=generation_id,
        keyframe__shot__storyboard__scene__project=project,
    ).first()
    if generation is None:
        raise StoryboardNotFound("Generation not found.")
    return generation_payload(generation, request=request)
