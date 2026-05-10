"""Public service facade matching the request spec's method signatures.

Lives between views and the lower-level service helpers in ``services.py``.
The shape is ``method(user, project_id, ...)`` so non-HTTP callers (workers,
admin scripts, tests) can use it without recreating DRF request objects.

Errors are raised as ``poster.errors.PosterError`` subclasses; the view layer
in ``dashboard_views.py`` translates them into the canonical HTTP response.
"""

from __future__ import annotations

from typing import Any, Optional

from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404

from w_craft_back.movie.poster.errors import (
    PosterJobNotFound,
    PosterVariantDeleted,
    PosterVariantNotFound,
    ProjectAccessDenied,
    ProjectNotFound,
)
from w_craft_back.movie.poster.models import (
    PosterGenerationJob,
    PosterVariant,
    ProjectPoster,
)
from w_craft_back.movie.poster.services import (
    complete_generation_mock,
    enqueue_generation_job,
    get_or_create_project_poster,
    list_recent_variants,
    resolve_reference_asset,
    select_variant as _select_variant,
    serialize_job,
    serialize_poster,
    serialize_variant,
    soft_delete_variant,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.permissions import (
    user_can_edit_project,
    user_has_project_access,
)


# Toggle to run the mock generator inline so the FE can iterate without a
# real worker. Drop this flag and dispatch to a queue from ``generate_poster``
# when the real pipeline lands.
RUN_MOCK_GENERATION_INLINE = True

DEFAULT_VARIANT_LIMIT = 8
MAX_VARIANT_LIMIT = 50


# --------------------------------------------------------------------------- #
# Access helpers
# --------------------------------------------------------------------------- #

def _load_project(project_id: int) -> Project:
    try:
        return Project.objects.select_related("owner", "user").get(pk=project_id)
    except Project.DoesNotExist as exc:
        raise ProjectNotFound("project not found") from exc


def _project_for_access(user: User, project_id: int) -> Project:
    project = _load_project(project_id)
    if not user_has_project_access(user, project):
        raise ProjectAccessDenied("you do not have access to this project")
    return project


def _project_for_edit(user: User, project_id: int) -> Project:
    project = _load_project(project_id)
    if not user_can_edit_project(user, project):
        raise ProjectAccessDenied("you do not have access to this project")
    return project


# --------------------------------------------------------------------------- #
# Public API (matches the spec's recommended service shape)
# --------------------------------------------------------------------------- #

def get_project_poster(
    user: User,
    project_id: int,
    *,
    request=None,
    limit: int = DEFAULT_VARIANT_LIMIT,
) -> dict[str, Any]:
    project = _project_for_access(user, project_id)
    poster = get_or_create_project_poster(project, user)
    recent = list_recent_variants(project, limit=limit)
    return {
        "poster": serialize_poster(
            poster, recent_variants=recent, request=request
        ),
        "recentVariants": [serialize_variant(v, request) for v in recent],
    }


def generate_poster(
    user: User,
    project_id: int,
    *,
    prompt: str,
    style: str,
    format: str,
    reference_image_url: str = "",
    reference_image_asset_id: Optional[int] = None,
    request=None,
    run_mock: bool = RUN_MOCK_GENERATION_INLINE,
) -> dict[str, Any]:
    project = _project_for_edit(user, project_id)

    reference_asset = resolve_reference_asset(project, reference_image_asset_id)
    poster, job = enqueue_generation_job(
        project=project,
        user=user,
        prompt=prompt,
        style=style,
        format=format,
        reference_image_url=reference_image_url or "",
        reference_asset=reference_asset,
    )

    if run_mock:
        complete_generation_mock(job)
        job.refresh_from_db()
        poster.refresh_from_db()

    recent = list_recent_variants(project, limit=DEFAULT_VARIANT_LIMIT)
    variants_for_job = list(
        PosterVariant.objects.filter(job=job, is_deleted=False)
        .order_by("variant_index", "created_at")
    )
    return {
        "jobId": job.id,
        "status": job.status,
        "job": serialize_job(job, request),
        "poster": serialize_poster(
            poster, recent_variants=recent, request=request
        ),
        "variants": [serialize_variant(v, request) for v in variants_for_job],
    }


def get_poster_job(
    user: User,
    project_id: int,
    job_id: int,
    *,
    request=None,
) -> dict[str, Any]:
    project = _project_for_access(user, project_id)
    try:
        job = PosterGenerationJob.objects.select_related("poster").get(
            pk=job_id, project=project
        )
    except PosterGenerationJob.DoesNotExist as exc:
        raise PosterJobNotFound("poster job not found") from exc

    variants = list(
        PosterVariant.objects.filter(job=job, is_deleted=False)
        .order_by("variant_index", "created_at")
    )
    return {
        "job": serialize_job(job, request),
        "variants": [serialize_variant(v, request) for v in variants],
    }


def select_poster_variant(
    user: User,
    project_id: int,
    variant_id: int,
    *,
    request=None,
) -> dict[str, Any]:
    project = _project_for_edit(user, project_id)

    variant = (
        PosterVariant.objects.filter(pk=variant_id, project=project).first()
    )
    if variant is None:
        raise PosterVariantNotFound("poster variant not found")
    if variant.is_deleted:
        raise PosterVariantDeleted("poster variant is deleted")

    poster = _select_variant(project=project, variant=variant)
    poster.refresh_from_db()
    variant.refresh_from_db()
    recent = list_recent_variants(project, limit=DEFAULT_VARIANT_LIMIT)

    return {
        "poster": {
            **serialize_poster(poster, recent_variants=recent, request=request),
            "selectedVariantId": poster.selected_variant_id,
        },
        "selectedVariant": serialize_variant(variant, request),
    }


def get_poster_variants(
    user: User,
    project_id: int,
    *,
    limit: int = DEFAULT_VARIANT_LIMIT,
    request=None,
) -> dict[str, Any]:
    project = _project_for_access(user, project_id)
    limit = max(1, min(MAX_VARIANT_LIMIT, int(limit or DEFAULT_VARIANT_LIMIT)))
    variants = list_recent_variants(project, limit=limit)
    return {"variants": [serialize_variant(v, request) for v in variants]}


def delete_poster_variant(
    user: User,
    project_id: int,
    variant_id: int,
    *,
    request=None,
) -> dict[str, Any]:
    project = _project_for_edit(user, project_id)

    variant = (
        PosterVariant.objects.filter(pk=variant_id, project=project).first()
    )
    if variant is None:
        raise PosterVariantNotFound("poster variant not found")

    if not variant.is_deleted:
        soft_delete_variant(variant=variant)

    return {"success": True}
