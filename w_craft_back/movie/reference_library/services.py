"""Application services for the project Reference Library."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
from collections.abc import Mapping
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from PIL import Image

from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.project import policy
from w_craft_back.movie.project.dashboard_models import (
    ActivityType,
    AssetType,
    Location,
    ProjectActivity,
    ProjectAsset,
    Scene,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.reference_library.errors import (
    ReferenceConflict,
    ReferenceError,
    ReferenceJobNotFound,
    ReferenceNotFound,
    ReferencePermissionDenied,
    ReferenceVariantNotFound,
    ReferenceVersionNotFound,
    map_provider_error,
    map_storage_error,
)
from w_craft_back.movie.reference_library.lifecycle import (
    cancel_reference_job,
    retry_reference_job,
)
from w_craft_back.movie.reference_library.models import (
    ProjectReference,
    ReferenceCategory,
    ReferenceCharacterLink,
    ReferenceGenerationJob,
    ReferenceJobStatus,
    ReferenceOperation,
    ReferenceSourceType,
    ReferenceVariant,
    ReferenceVariantStatus,
    ReferenceVersion,
    SceneReference,
)
from w_craft_back.movie.reference_library.prompt_compiler import (
    ALLOWED_ASPECT_RATIOS,
    BRIEF_SCHEMA_VERSION,
    compile_reference_prompt,
    normalize_brief,
)
from w_craft_back.movie.reference_library.providers import (
    effective_reference_model_key,
    provider_mode,
    resolve_reference_provider,
)
from w_craft_back.services.image_generation.errors import ImageProviderError
from w_craft_back.credits.pricing import estimate_for_pinned_provider
from w_craft_back.credits.services import (
    CreditServiceError,
    generation_charge_payload,
    reserve_generation,
)
from w_craft_back.storage_gateway import (
    NormalizedImage,
    StorageGatewayError,
    delete_storage_key,
    normalize_image_bytes,
    normalize_image_upload,
    signed_url_for_file,
    store_normalized_image,
)


NON_TERMINAL_STATUSES = (
    ReferenceJobStatus.QUEUED,
    ReferenceJobStatus.PROCESSING,
    ReferenceJobStatus.CANCELLATION_REQUESTED,
)
RIGHTS_STATEMENT_VERSION = "reference-upload-v1"


def _project_for_action(actor: Any, project_id: int, action: policy.Action) -> Project:
    project = Project.objects.filter(pk=project_id).first()
    if project is None or not policy.can(actor, project, action):
        code = (
            "PROJECT_ACCESS_DENIED"
            if action == policy.Action.VIEW
            else (
                "REFERENCE_GENERATION_FORBIDDEN"
                if action == policy.Action.RUN_GENERATION
                else "REFERENCE_EDIT_FORBIDDEN"
            )
        )
        raise ReferencePermissionDenied("Project access denied.", code=code)
    return project


def _reference_for_project(
    project: Project,
    reference_id: uuid.UUID,
    *,
    lock: bool = False,
) -> ProjectReference:
    queryset = ProjectReference.objects
    if lock:
        queryset = queryset.select_for_update()
    else:
        queryset = queryset.select_related("active_version")
    reference = queryset.filter(
        pk=reference_id,
        project=project,
    ).first()
    if reference is None:
        raise ReferenceNotFound("Reference not found.")
    return reference


def _ensure_version(reference: ProjectReference, expected: int) -> None:
    if reference.version != expected:
        raise ReferenceConflict(
            "Reference was changed by another editor.",
            code="REFERENCE_VERSION_CONFLICT",
            retryable=True,
            current_version=reference.version,
        )


def _normalize_tags(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(str(value).split())
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result[:20]


def _record_activity(
    reference: ProjectReference,
    actor: Any,
    event: str,
    *,
    activity_type: str = ActivityType.PROJECT_UPDATED,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    safe_metadata = {"event": event, "referenceId": str(reference.id)}
    safe_metadata.update(dict(metadata or {}))
    ProjectActivity.objects.create(
        project=reference.project,
        user=actor,
        activity_type=activity_type,
        title=reference.title,
        description=event.replace("_", " ")[:500],
        target_type="reference",
        target_id=str(reference.id),
        metadata=safe_metadata,
    )


def _computed_status(reference: ProjectReference) -> str:
    if reference.archived_at:
        return "archived"
    latest_job = reference.generation_jobs.order_by("-created_at").first()
    if latest_job and latest_job.status in NON_TERMINAL_STATUSES:
        return "generating"
    if reference.active_version_id:
        return "ready"
    if latest_job and latest_job.status == ReferenceJobStatus.FAILED:
        return "failed"
    return "draft"


def _version_payload(
    version: ReferenceVersion | None,
    request=None,
) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": str(version.id),
        "number": version.version_number,
        "origin": version.source_type,
        "imageUrl": signed_url_for_file(
            version.asset.file,
            request,
            project=version.reference.project,
        ),
        "thumbnailUrl": signed_url_for_file(
            (
                version.thumbnail_asset.file
                if version.thumbnail_asset_id
                else version.asset.file
            ),
            request,
            project=version.reference.project,
        ),
        "provider": version.provider or None,
        "modelName": version.model_name or None,
        "createdById": version.created_by_id,
        "createdAt": version.created_at.isoformat(),
    }


def _character_links_payload(reference: ProjectReference) -> list[dict[str, Any]]:
    links = reference.character_links.select_related("character").order_by("id")
    return [
        {
            "characterId": str(link.character_id),
            "name": link.character.name,
            "relation": link.relation,
            "note": link.note,
        }
        for link in links
    ]


def _reference_payload(
    reference: ProjectReference,
    request=None,
    *,
    detail: bool = False,
) -> dict[str, Any]:
    status_value = _computed_status(reference)
    latest_job = reference.generation_jobs.order_by("-created_at").first()
    warning = None
    if (
        reference.active_version_id
        and latest_job
        and latest_job.status == ReferenceJobStatus.FAILED
    ):
        warning = {
            "code": latest_job.error_code or "REFERENCE_GENERATION_FAILED",
            "detail": (
                latest_job.error_detail
                or "The latest generation attempt failed."
            ),
            "retryable": bool(latest_job.error_retryable),
        }
    characters = _character_links_payload(reference)
    payload: dict[str, Any] = {
        "id": str(reference.id),
        "title": reference.title,
        "category": reference.category,
        "categoryLabel": reference.get_category_display(),
        "status": status_value,
        "activeVersion": _version_payload(reference.active_version, request),
        "tags": reference.tags or [],
        "usage": {
            "sceneCount": reference.scene_usages.count(),
            "characters": characters,
        },
        "lastJobWarning": warning,
        "version": reference.version,
        "archivedAt": (
            reference.archived_at.isoformat() if reference.archived_at else None
        ),
        "updatedAt": reference.updated_at.isoformat(),
    }
    if detail:
        payload.update(
            {
                "description": reference.description,
                "brief": reference.brief or {},
                "locationId": reference.location_id,
                "characterLinks": characters,
                "createdAt": reference.created_at.isoformat(),
            }
        )
    return payload


def get_capabilities(*, actor: Any, project_id: int) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    mode = provider_mode()
    permissions = policy.permission_summary(actor, project)
    effective_model = "mock"
    configured = True
    can_edit_provider = True
    if mode == "registry":
        project_model = str(
            (project.generation_settings or {}).get("image_generation_model", "") or ""
        ).strip()
        try:
            provider = resolve_reference_provider(
                actor=actor,
                project=project,
                requested_model=project_model,
            )
            effective_model = provider.model_id
            can_edit_provider = provider.supports_edit()
        except ReferenceError:
            configured = False
            effective_model = project_model or None
        except ImageProviderError:
            configured = False
            effective_model = project_model or None
    return {
        "permissions": {
            "canView": permissions["canView"],
            "canEdit": permissions["canEdit"],
            "canRunGeneration": permissions["canRunGeneration"],
        },
        "categories": [
            {"key": key, "label": label} for key, label in ReferenceCategory.choices
        ],
        "generation": {
            "configured": configured,
            "providerMode": mode,
            "effectiveModel": effective_model,
            "canGenerate": configured and permissions["canRunGeneration"],
            "canEdit": (
                configured
                and can_edit_provider
                and permissions["canRunGeneration"]
            ),
            "generateVariantCounts": [1, 2, 4],
            "editVariantCounts": [1],
            "aspectRatios": list(ALLOWED_ASPECT_RATIOS),
        },
        "upload": {
            "maxBytes": 10 * 1024 * 1024,
            "maxPixels": 20_000_000,
            "mimeTypes": ["image/jpeg", "image/png", "image/webp"],
            "rightsStatementVersion": RIGHTS_STATEMENT_VERSION,
        },
    }


def list_references(
    *,
    actor: Any,
    project_id: int,
    request=None,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    queryset = ProjectReference.objects.filter(project=project).select_related(
        "active_version__asset",
        "active_version__thumbnail_asset",
    )
    status_filter = str(params.get("status", "") or "").strip()
    if status_filter and status_filter not in {
        "archived",
        "draft",
        "generating",
        "ready",
        "failed",
    }:
        raise ReferenceError("Invalid status.", code="REFERENCE_INVALID_BRIEF")
    if status_filter == "archived":
        queryset = queryset.filter(archived_at__isnull=False)
    elif not str(params.get("includeArchived", "")).lower() in {"1", "true"}:
        queryset = queryset.filter(archived_at__isnull=True)
    category = str(params.get("category", "") or "").strip()
    if category:
        if category not in ReferenceCategory.values:
            raise ReferenceError("Invalid category.", code="REFERENCE_INVALID_CATEGORY")
        queryset = queryset.filter(category=category)
    search = str(params.get("search", "") or "").strip()[:255]
    if search:
        queryset = queryset.filter(
            Q(title__icontains=search) | Q(description__icontains=search)
        )
    if params.get("character"):
        queryset = queryset.filter(character_links__character_id=params["character"])
    if params.get("location"):
        queryset = queryset.filter(location_id=params["location"])
    if params.get("scene"):
        queryset = queryset.filter(scene_usages__scene_id=params["scene"])
    if status_filter == "ready":
        queryset = queryset.filter(
            active_version__isnull=False,
            archived_at__isnull=True,
        ).exclude(
            generation_jobs__status__in=NON_TERMINAL_STATUSES,
        )
    ordering = {
        "updatedAt": "updated_at",
        "-updatedAt": "-updated_at",
        "createdAt": "created_at",
        "-createdAt": "-created_at",
        "title": "title",
        "-title": "-title",
    }.get(str(params.get("ordering", "-updatedAt")), "-updated_at")
    ordered_queryset = queryset.distinct().order_by(ordering)
    requires_computed_filter = status_filter in {"draft", "generating", "failed"}
    if requires_computed_filter:
        rows = list(ordered_queryset)
        rows = [
            reference
            for reference in rows
            if _computed_status(reference) == status_filter
        ]
    try:
        page = max(1, int(params.get("page", 1)))
        page_size = max(1, min(100, int(params.get("pageSize", 24))))
    except (TypeError, ValueError) as exc:
        raise ReferenceError(
            "Invalid pagination.",
            code="REFERENCE_INVALID_BRIEF",
        ) from exc
    start = (page - 1) * page_size
    if requires_computed_filter:
        total = len(rows)
        rows = rows[start:start + page_size]
    else:
        total = ordered_queryset.count()
        rows = list(ordered_queryset[start:start + page_size])
    return {
        "items": [
            _reference_payload(reference, request)
            for reference in rows
        ],
        "page": page,
        "pageSize": page_size,
        "total": total,
    }


def get_link_options(*, actor: Any, project_id: int) -> dict[str, list[dict[str, Any]]]:
    """Return compact project-scoped entities available for reference links."""

    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    characters = StudioCharacter.objects.filter(project=project).order_by(
        "name",
        "character_id",
    )
    locations = Location.objects.filter(project=project).order_by("name", "id")
    return {
        "characters": [
            {"id": str(character.character_id), "name": character.name}
            for character in characters
        ],
        "locations": [
            {"id": location.id, "name": location.name}
            for location in locations
        ],
    }


def _location_for_reference(
    project: Project,
    location_id: int | None,
) -> Location | None:
    if location_id is None:
        return None
    location = Location.objects.filter(pk=location_id, project=project).first()
    if location is None:
        raise ReferenceError(
            "Location belongs to another project or does not exist.",
            code="REFERENCE_CROSS_PROJECT_LINK",
        )
    return location


def _replace_character_links(
    reference: ProjectReference,
    links: list[Mapping[str, Any]],
) -> None:
    character_ids = {item["characterId"] for item in links}
    characters = {
        character.character_id: character
        for character in StudioCharacter.objects.filter(
            project=reference.project,
            character_id__in=character_ids,
        )
    }
    if set(characters) != character_ids:
        raise ReferenceError(
            "Character belongs to another project or does not exist.",
            code="REFERENCE_CROSS_PROJECT_LINK",
        )
    reference.character_links.all().delete()
    ReferenceCharacterLink.objects.bulk_create(
        [
            ReferenceCharacterLink(
                reference=reference,
                character=characters[item["characterId"]],
                relation=item["relation"],
                note=item.get("note", ""),
            )
            for item in links
        ]
    )


@transaction.atomic
def create_reference(
    *,
    actor: Any,
    project_id: int,
    data: Mapping[str, Any],
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.EDIT_CONTENT)
    category = data["category"]
    location = _location_for_reference(project, data.get("locationId"))
    reference = ProjectReference.objects.create(
        project=project,
        title=data["title"].strip(),
        category=category,
        description=data.get("description", "").strip(),
        brief=normalize_brief(data.get("brief", {})),
        tags=_normalize_tags(data.get("tags", [])),
        location=location,
        created_by=actor,
        updated_by=actor,
    )
    _replace_character_links(reference, data.get("characterLinks", []))
    _record_activity(reference, actor, "reference_created")
    return _reference_payload(reference, request, detail=True)


def get_reference(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    reference = _reference_for_project(project, reference_id)
    return _reference_payload(reference, request, detail=True)


@transaction.atomic
def update_reference(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    data: Mapping[str, Any],
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.EDIT_CONTENT)
    reference = _reference_for_project(project, reference_id, lock=True)
    _ensure_version(reference, data["version"])
    category = data.get("category", reference.category)
    location_id = data.get("locationId", reference.location_id)
    location = _location_for_reference(project, location_id)
    for field in ("title", "description"):
        if field in data:
            setattr(reference, field, data[field].strip())
    reference.category = category
    reference.location = location
    if "brief" in data:
        reference.brief = normalize_brief(data["brief"])
    if "tags" in data:
        reference.tags = _normalize_tags(data["tags"])
    if "characterLinks" in data:
        _replace_character_links(reference, data["characterLinks"])
    reference.version += 1
    reference.updated_by = actor
    reference.save()
    _record_activity(reference, actor, "reference_updated")
    return _reference_payload(reference, request, detail=True)


@transaction.atomic
def set_archived(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    expected_version: int,
    archived: bool,
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.EDIT_CONTENT)
    reference = _reference_for_project(project, reference_id, lock=True)
    _ensure_version(reference, expected_version)
    if archived and reference.generation_jobs.filter(
        status__in=NON_TERMINAL_STATUSES
    ).exists():
        raise ReferenceConflict(
            "An active generation job must finish or be cancelled first.",
            code="REFERENCE_JOB_ALREADY_ACTIVE",
            retryable=True,
        )
    reference.archived_at = timezone.now() if archived else None
    reference.version += 1
    reference.updated_by = actor
    reference.save(update_fields=["archived_at", "version", "updated_by", "updated_at"])
    _record_activity(
        reference,
        actor,
        "reference_archived" if archived else "reference_restored",
    )
    return _reference_payload(reference, request, detail=True)


def list_versions(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    reference = _reference_for_project(project, reference_id)
    versions = reference.versions.select_related(
        "asset",
        "thumbnail_asset",
        "created_by",
    )
    return {
        "items": [_version_payload(version, request) for version in versions],
        "activeVersionId": (
            str(reference.active_version_id) if reference.active_version_id else None
        ),
    }


def _thumbnail(image: NormalizedImage) -> NormalizedImage:
    with Image.open(io.BytesIO(image.data)) as source:
        thumb = source.convert("RGB")
        thumb.thumbnail((512, 512))
        output = io.BytesIO()
        thumb.save(output, format="WEBP", quality=82, method=4)
    return normalize_image_bytes(output.getvalue())


def _create_project_asset(
    *,
    project: Project,
    actor: Any,
    stored: Any,
    asset_type: str,
    title: str,
    role: str,
    origin: str,
    reference_job_id: uuid.UUID | str | None = None,
) -> ProjectAsset:
    metadata = {
        "domain": "reference_library",
        "role": role,
        "origin": origin,
        "mime_type": stored.mime_type,
        "size_bytes": stored.size_bytes,
        "sha256": stored.sha256,
        "width": stored.width,
        "height": stored.height,
    }
    if reference_job_id is not None:
        metadata["reference_job_id"] = str(reference_job_id)
    return ProjectAsset.objects.create(
        project=project,
        uploaded_by=actor,
        file=stored.storage_key,
        asset_type=asset_type,
        title=str(title)[:255],
        metadata=metadata,
    )


def persist_reference_image_pair(
    *,
    project: Project,
    actor: Any,
    image: NormalizedImage,
    title: str,
    origin: str,
    reference_job_id: uuid.UUID | str | None = None,
) -> tuple[ProjectAsset, ProjectAsset, list[str]]:
    """Store original + thumbnail and create their ProjectAsset rows."""

    stored_keys: list[str] = []
    try:
        original = store_normalized_image(
            image,
            namespace=f"projects/{project.id}/references/originals",
        )
        stored_keys.append(original.storage_key)
        thumbnail = store_normalized_image(
            _thumbnail(image),
            namespace=f"projects/{project.id}/references/thumbnails",
        )
        stored_keys.append(thumbnail.storage_key)
        with transaction.atomic():
            original_asset = _create_project_asset(
                project=project,
                actor=actor,
                stored=original,
                asset_type=AssetType.REFERENCE,
                title=title,
                role="source",
                origin=origin,
                reference_job_id=reference_job_id,
            )
            thumbnail_asset = _create_project_asset(
                project=project,
                actor=actor,
                stored=thumbnail,
                asset_type=AssetType.IMAGE,
                title=f"{title} — thumbnail",
                role="thumbnail",
                origin=origin,
                reference_job_id=reference_job_id,
            )
        return original_asset, thumbnail_asset, stored_keys
    except StorageGatewayError as error:
        for key in stored_keys:
            delete_storage_key(key)
        raise map_storage_error(error) from error
    except Exception:
        for key in stored_keys:
            delete_storage_key(key)
        raise


@transaction.atomic
def upload_version(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    upload: Any,
    expected_version: int,
    rights_statement_version: str,
    request=None,
) -> dict[str, Any]:
    if rights_statement_version != RIGHTS_STATEMENT_VERSION:
        raise ReferenceError(
            "Usage rights statement is missing or outdated.",
            code="REFERENCE_UPLOAD_RIGHTS_REQUIRED",
        )
    project = _project_for_action(actor, project_id, policy.Action.EDIT_CONTENT)
    reference = _reference_for_project(project, reference_id, lock=True)
    _ensure_version(reference, expected_version)
    if reference.archived_at:
        raise ReferenceConflict(
            "Archived references cannot be changed.",
            code="REFERENCE_ARCHIVED",
        )
    try:
        image = normalize_image_upload(upload)
    except StorageGatewayError as error:
        raise map_storage_error(error) from error
    stored_keys: list[str] = []
    try:
        asset, thumbnail_asset, stored_keys = persist_reference_image_pair(
            project=project,
            actor=actor,
            image=image,
            title=reference.title,
            origin=ReferenceSourceType.UPLOAD,
        )
        next_number = (
            reference.versions.order_by("-version_number")
            .values_list("version_number", flat=True)
            .first()
            or 0
        ) + 1
        version = ReferenceVersion.objects.create(
            reference=reference,
            version_number=next_number,
            asset=asset,
            thumbnail_asset=thumbnail_asset,
            source_type=ReferenceSourceType.UPLOAD,
            brief_snapshot=reference.brief,
            rights_confirmed_by=actor,
            rights_confirmed_at=timezone.now(),
            rights_statement_version=rights_statement_version,
            created_by=actor,
        )
        reference.active_version = version
        reference.version += 1
        reference.updated_by = actor
        reference.save(
            update_fields=[
                "active_version",
                "version",
                "updated_by",
                "updated_at",
            ]
        )
        _record_activity(
            reference,
            actor,
            "reference_uploaded",
            activity_type=ActivityType.ASSET_UPLOADED,
            metadata={"versionId": str(version.id)},
        )
    except Exception:
        for key in stored_keys:
            delete_storage_key(key)
        raise
    return {
        "referenceId": str(reference.id),
        "referenceVersion": reference.version,
        "activeVersion": _version_payload(version, request),
    }


def _fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _job_payload(
    job: ReferenceGenerationJob,
    request=None,
    *,
    detail: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(job.id),
        "referenceId": str(job.reference_id),
        "operation": job.operation,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "variantCount": job.variant_count,
        "attempts": job.attempts,
        "canCancel": job.status == ReferenceJobStatus.QUEUED,
        "canRetry": (
            job.status in (ReferenceJobStatus.FAILED, ReferenceJobStatus.CANCELLED)
            and job.attempts < job.max_attempts
            and job.reference.archived_at is None
        ),
        "error": (
            {
                "code": job.error_code,
                "detail": job.error_detail,
                "retryable": bool(job.error_retryable),
            }
            if job.error_code
            else None
        ),
        "createdAt": job.created_at.isoformat(),
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
        "billing": generation_charge_payload("reference", str(job.id)),
    }
    if detail:
        variants = job.variants.select_related(
            "asset",
            "thumbnail_asset",
        ).order_by("variant_index")
        payload["variants"] = [
            {
                "id": str(variant.id),
                "index": variant.variant_index,
                "status": variant.status,
                "imageUrl": signed_url_for_file(
                    variant.asset.file,
                    request,
                    project=job.project,
                ),
                "thumbnailUrl": signed_url_for_file(
                    (
                        variant.thumbnail_asset.file
                        if variant.thumbnail_asset_id
                        else variant.asset.file
                    ),
                    request,
                    project=job.project,
                ),
                "width": variant.asset.metadata.get("width"),
                "height": variant.asset.metadata.get("height"),
            }
            for variant in variants
        ]
    return payload


@transaction.atomic
def enqueue_job(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    data: Mapping[str, Any],
    idempotency_key: str,
    request=None,
) -> tuple[dict[str, Any], bool]:
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or any(ord(char) < 32 for char in idempotency_key)
    ):
        raise ReferenceError(
            "A valid Idempotency-Key header is required.",
            code="REFERENCE_IDEMPOTENCY_MISMATCH",
        )
    project = _project_for_action(actor, project_id, policy.Action.RUN_GENERATION)
    reference = _reference_for_project(project, reference_id, lock=True)
    request_snapshot = {
        "referenceId": str(reference.id),
        "operation": data["operation"],
        "sourceVersionId": str(data.get("sourceVersionId") or ""),
        "variantCount": data["variantCount"],
        "imageModel": data.get("imageModel", ""),
        "routingMode": data.get("routingMode", "manual"),
        "brief": data.get("brief", reference.brief),
        "editInstruction": data.get("editInstruction", ""),
        "expectedReferenceVersion": data["expectedReferenceVersion"],
    }
    fingerprint = _fingerprint(request_snapshot)
    existing = ReferenceGenerationJob.objects.filter(
        project=project,
        actor=actor,
        idempotency_key=idempotency_key,
    ).first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise ReferenceConflict(
                "Idempotency key was already used with a different request.",
                code="REFERENCE_IDEMPOTENCY_MISMATCH",
            )
        return _job_payload(existing, request, detail=True), False
    _ensure_version(reference, data["expectedReferenceVersion"])
    if reference.archived_at:
        raise ReferenceConflict(
            "Archived references cannot be generated.",
            code="REFERENCE_ARCHIVED",
        )
    if reference.generation_jobs.filter(status__in=NON_TERMINAL_STATUSES).exists():
        raise ReferenceConflict(
            "A generation job is already active for this reference.",
            code="REFERENCE_JOB_ALREADY_ACTIVE",
            retryable=True,
        )
    operation = data["operation"]
    source_version = None
    if operation == ReferenceOperation.EDIT:
        source_version = reference.versions.filter(
            pk=data.get("sourceVersionId")
        ).first()
        if source_version is None:
            raise ReferenceVersionNotFound("Source version not found.")
    brief = normalize_brief(data.get("brief", reference.brief))
    compiled = compile_reference_prompt(
        category=reference.category,
        description=reference.description,
        brief=brief,
        edit_instruction=data.get("editInstruction", ""),
    )
    effective_model = effective_reference_model_key(
        actor=actor,
        project=project,
        requested_model=data.get("imageModel", ""),
    )
    routing_mode = str(data.get("routingMode") or "manual").lower()
    try:
        if routing_mode != "manual":
            from w_craft_back.services.image_generation.routing import (
                build_routing_decision,
            )

            decision = build_routing_decision(
                mode=routing_mode,
                requested_model=effective_model,
                operation=(
                    "edit" if operation == ReferenceOperation.EDIT else "generate"
                ),
                variant_count=data["variantCount"],
                prompt=compiled.compiled_prompt,
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
                require_edit=operation == ReferenceOperation.EDIT,
            )
            provider_snapshot = (
                {"spec": provider.spec.__dict__}
                if getattr(provider, "spec", None) is not None
                else {}
            )
            provider_name = provider.name
            model_name = provider.model_id
    except ImageProviderError as error:
        raise map_provider_error(error) from error
    except CreditServiceError as error:
        raise ReferenceError(
            error.message,
            code=error.code,
            http_status=error.http_status,
        ) from error
    try:
        job = ReferenceGenerationJob.objects.create(
            project=project,
            reference=reference,
            actor=actor,
            operation=operation,
            brief_snapshot=brief,
            compiled_request={
                "schemaVersion": BRIEF_SCHEMA_VERSION,
                "compiledPrompt": compiled.compiled_prompt,
                "negativePrompt": compiled.negative_prompt,
                "metadata": compiled.metadata,
                "editInstruction": data.get("editInstruction", ""),
            },
            source_version=source_version,
            variant_count=data["variantCount"],
            requested_model=effective_model,
            provider_snapshot=provider_snapshot,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            provider=provider_name,
            model_name=model_name,
        )
        try:
            if provider_snapshot.get("candidates"):
                from w_craft_back.services.image_generation.routing import (
                    estimate_route_snapshot,
                )

                estimate, reservation_amount, pricing_snapshot = (
                    estimate_route_snapshot(
                        provider_snapshot,
                        operation=(
                            "edit"
                            if operation == ReferenceOperation.EDIT
                            else "generate"
                        ),
                        variant_count=data["variantCount"],
                        prompt=compiled.compiled_prompt,
                    )
                )
            else:
                estimate = estimate_for_pinned_provider(
                    provider=provider_name,
                    provider_snapshot=provider_snapshot or None,
                    model_name=model_name,
                    operation=(
                        "edit"
                        if operation == ReferenceOperation.EDIT
                        else "generate"
                    ),
                    variant_count=data["variantCount"],
                    prompt=compiled.compiled_prompt,
                )
                reservation_amount = estimate.reservation_amount
                pricing_snapshot = estimate.snapshot
            reserve_generation(
                user=actor,
                domain="reference",
                job_id=str(job.id),
                provider=estimate.provider,
                model_name=estimate.model_name,
                estimated_cost=estimate.estimated_cost,
                reservation_amount=reservation_amount,
                pricing_snapshot=pricing_snapshot,
                project=project,
                operation=operation,
                routing_mode=routing_mode,
            )
        except CreditServiceError as error:
            raise ReferenceError(
                error.message,
                code=error.code,
                http_status=error.http_status,
            ) from error
    except IntegrityError as error:
        raise ReferenceConflict(
            "A generation job is already active for this reference.",
            code="REFERENCE_JOB_ALREADY_ACTIVE",
            retryable=True,
        ) from error
    return _job_payload(job, request, detail=True), True


def list_jobs(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    reference = _reference_for_project(project, reference_id)
    jobs = reference.generation_jobs.all()[:100]
    return {"items": [_job_payload(job, request) for job in jobs]}


def get_job(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    job_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    job = ReferenceGenerationJob.objects.select_related("reference", "project").filter(
        pk=job_id,
        reference_id=reference_id,
        project=project,
    ).first()
    if job is None:
        raise ReferenceJobNotFound("Generation job not found.")
    return _job_payload(job, request, detail=True)


def cancel_job_service(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    job_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.RUN_GENERATION)
    job = ReferenceGenerationJob.objects.filter(
        pk=job_id,
        reference_id=reference_id,
        project=project,
    ).first()
    if job is None:
        raise ReferenceJobNotFound("Generation job not found.")
    return _job_payload(cancel_reference_job(job.id), request, detail=True)


def retry_job_service(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    job_id: uuid.UUID,
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.RUN_GENERATION)
    job = ReferenceGenerationJob.objects.filter(
        pk=job_id,
        reference_id=reference_id,
        project=project,
    ).first()
    if job is None:
        raise ReferenceJobNotFound("Generation job not found.")
    retried = retry_reference_job(job.id, actor=actor)
    return _job_payload(retried, request, detail=True)


@transaction.atomic
def apply_variant(
    *,
    actor: Any,
    project_id: int,
    reference_id: uuid.UUID,
    job_id: uuid.UUID,
    variant_id: uuid.UUID,
    expected_version: int,
    request=None,
) -> tuple[dict[str, Any], bool]:
    project = _project_for_action(actor, project_id, policy.Action.EDIT_CONTENT)
    reference = _reference_for_project(project, reference_id, lock=True)
    variant = ReferenceVariant.objects.select_for_update().filter(
        pk=variant_id,
        job_id=job_id,
        job__reference=reference,
    ).first()
    if variant is None:
        raise ReferenceVariantNotFound("Variant not found.")
    if variant.status == ReferenceVariantStatus.APPLIED:
        existing = ReferenceVersion.objects.filter(source_variant=variant).first()
        if existing is not None and reference.active_version_id == existing.id:
            return {
                "referenceId": str(reference.id),
                "referenceVersion": reference.version,
                "activeVersion": _version_payload(existing, request),
            }, False
        raise ReferenceConflict(
            "Variant was already applied and is no longer active.",
            code="REFERENCE_VARIANT_ALREADY_APPLIED",
        )
    _ensure_version(reference, expected_version)
    if reference.archived_at:
        raise ReferenceConflict(
            "Archived references cannot be changed.",
            code="REFERENCE_ARCHIVED",
        )
    if variant.job.status != ReferenceJobStatus.COMPLETED:
        raise ReferenceConflict(
            "Generation job is not completed.",
            code="REFERENCE_JOB_NOT_COMPLETED",
            retryable=True,
        )
    next_number = (
        reference.versions.order_by("-version_number")
        .values_list("version_number", flat=True)
        .first()
        or 0
    ) + 1
    source_type = (
        ReferenceSourceType.EDIT
        if variant.job.operation == ReferenceOperation.EDIT
        else ReferenceSourceType.GENERATED
    )
    version = ReferenceVersion.objects.create(
        reference=reference,
        version_number=next_number,
        asset=variant.asset,
        thumbnail_asset=variant.thumbnail_asset,
        source_type=source_type,
        source_variant=variant,
        brief_snapshot=variant.job.brief_snapshot,
        compiled_prompt=variant.job.compiled_request.get("compiledPrompt", ""),
        negative_prompt=variant.job.compiled_request.get("negativePrompt", ""),
        provider=variant.job.provider,
        model_name=variant.job.model_name,
        seed=variant.seed,
        created_by=actor,
    )
    variant.status = ReferenceVariantStatus.APPLIED
    variant.applied_at = timezone.now()
    variant.save(update_fields=["status", "applied_at"])
    reference.active_version = version
    reference.version += 1
    reference.updated_by = actor
    reference.save(
        update_fields=[
            "active_version",
            "version",
            "updated_by",
            "updated_at",
        ]
    )
    _record_activity(
        reference,
        actor,
        "reference_variant_applied",
        metadata={"versionId": str(version.id)},
    )
    return {
        "referenceId": str(reference.id),
        "referenceVersion": reference.version,
        "activeVersion": _version_payload(version, request),
    }, True


def _scene_item_payload(item: SceneReference, request=None) -> dict[str, Any]:
    active = item.reference.active_version
    return {
        "referenceId": str(item.reference_id),
        "title": item.reference.title,
        "category": item.reference.category,
        "versionId": str(item.version_id),
        "versionNumber": item.version.version_number,
        "usage": item.usage,
        "note": item.note,
        "thumbnailUrl": signed_url_for_file(
            (
                item.version.thumbnail_asset.file
                if item.version.thumbnail_asset_id
                else item.version.asset.file
            ),
            request,
            project=item.scene.project,
        ),
        "updateAvailable": bool(active and active.id != item.version_id),
        "activeVersionId": str(active.id) if active else None,
    }


def get_scene_references(
    *, actor: Any, project_id: int, scene_id: int, request=None
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    scene = Scene.objects.filter(pk=scene_id, project=project).first()
    if scene is None:
        raise ReferenceError(
            "Scene not found.",
            code="REFERENCE_CROSS_PROJECT_LINK",
            http_status=404,
        )
    items = SceneReference.objects.filter(scene=scene).select_related(
        "scene",
        "reference__active_version",
        "version__asset",
        "version__thumbnail_asset",
    )
    return {
        "sceneId": scene.id,
        "sceneVersion": scene.version,
        "items": [_scene_item_payload(item, request) for item in items],
    }


@transaction.atomic
def replace_scene_references(
    *,
    actor: Any,
    project_id: int,
    scene_id: int,
    expected_scene_version: int,
    items: list[Mapping[str, Any]],
    request=None,
) -> dict[str, Any]:
    project = _project_for_action(actor, project_id, policy.Action.EDIT_CONTENT)
    scene = (
        Scene.objects.select_for_update()
        .filter(pk=scene_id, project=project)
        .first()
    )
    if scene is None:
        raise ReferenceError(
            "Scene not found.",
            code="REFERENCE_CROSS_PROJECT_LINK",
            http_status=404,
        )
    if scene.version != expected_scene_version:
        raise ReferenceConflict(
            "Scene was changed by another editor.",
            code="REFERENCE_VERSION_CONFLICT",
            retryable=True,
            current_version=scene.version,
        )
    reference_ids = {item["referenceId"] for item in items}
    references = {
        reference.id: reference
        for reference in ProjectReference.objects.filter(
            project=project,
            id__in=reference_ids,
        ).select_related("active_version")
    }
    if set(references) != reference_ids:
        raise ReferenceError(
            "Reference belongs to another project.",
            code="REFERENCE_CROSS_PROJECT_LINK",
        )
    version_ids = {item["versionId"] for item in items}
    versions = {
        version.id: version
        for version in ReferenceVersion.objects.filter(
            id__in=version_ids,
            reference__project=project,
        ).select_related("asset", "thumbnail_asset")
    }
    if set(versions) != version_ids:
        raise ReferenceError(
            "Version belongs to another project.",
            code="REFERENCE_CROSS_PROJECT_LINK",
        )
    for item in items:
        reference = references[item["referenceId"]]
        version = versions[item["versionId"]]
        if reference.archived_at:
            raise ReferenceConflict(
                "Archived references cannot be assigned.",
                code="REFERENCE_ARCHIVED",
            )
        if version.reference_id != reference.id:
            raise ReferenceError(
                "Version does not belong to reference.",
                code="REFERENCE_CROSS_PROJECT_LINK",
            )
    SceneReference.objects.filter(scene=scene).delete()
    SceneReference.objects.bulk_create(
        [
            SceneReference(
                scene=scene,
                reference=references[item["referenceId"]],
                version=versions[item["versionId"]],
                usage=item["usage"],
                note=item.get("note", ""),
                created_by=actor,
            )
            for item in items
        ]
    )
    scene.version += 1
    scene.updated_by = actor
    scene.save(update_fields=["version", "updated_by", "updated_at"])
    ProjectActivity.objects.create(
        project=project,
        user=actor,
        activity_type=ActivityType.PROJECT_UPDATED,
        title=scene.title,
        description="scene references updated",
        target_type="scene",
        target_id=str(scene.id),
        metadata={
            "event": "scene_references_replaced",
            "sceneId": scene.id,
            "count": len(items),
        },
    )
    return get_scene_references(
        actor=actor,
        project_id=project.id,
        scene_id=scene.id,
        request=request,
    )
