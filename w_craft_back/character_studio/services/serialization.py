from django.forms.models import model_to_dict

from w_craft_back.storage_gateway import signed_url_for_asset


def value_to_json(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value.__class__.__name__ == "UUID" else value


def model_dict(instance, fields=None):
    if instance is None:
        return None
    data = model_to_dict(instance, fields=fields)
    for key, value in list(data.items()):
        data[key] = value_to_json(value)
    return data


def public_url(value):
    """Sign legacy local media URLs; retain non-storage external URLs."""

    if not value or not isinstance(value, str):
        return value
    signed = signed_url_for_asset(storage_key=None, legacy_url=value)
    return signed or value


def _character_asset_url(asset):
    if asset is None:
        return None
    return signed_url_for_asset(
        storage_key=asset.storage_path,
        legacy_url=asset.image_url,
    )


def appearance_dict(appearance):
    return model_dict(appearance)


def outfit_dict(outfit):
    data = model_dict(outfit)
    if data:
        ref = outfit.reference_image
        data["reference_image_url"] = _character_asset_url(ref)
        data["reference_image_asset_id"] = str(ref.asset_id) if ref else None
    return data


def asset_dict(asset):
    data = model_dict(asset)
    if data:
        data["image_url"] = _character_asset_url(asset)
    return data


def reference_dict(asset, reference_type):
    """Serialize a CharacterAsset as a row of the References screen.

    `reference_type` is the UI vocabulary (portrait, full_body, three_quarter,
    profile, back_view, emotions, poses, outfit_details, character_sheet) and
    is included in the row even when the asset is missing — the response
    always contains all 9 reference types in stable order.
    """
    if asset is None:
        return {
            "reference_type": reference_type,
            "status": "missing",
            "asset_id": None,
            "image_url": None,
            "is_primary": False,
            "version": 0,
            "source": None,
            "correction_prompt": "",
            "error_message": "",
            "updated_at": None,
        }
    return {
        "reference_type": reference_type,
        "status": asset.status,
        "asset_id": str(asset.asset_id),
        "image_url": _character_asset_url(asset),
        "thumbnail_url": _character_asset_url(asset),
        "is_primary": bool(asset.is_primary),
        "version": int(asset.version or 1),
        "source": asset.source or "",
        "generation_job_id": str(asset.source_job_id) if asset.source_job_id else None,
        "correction_prompt": asset.correction_prompt or "",
        "error_message": asset.error_message or "",
        "updated_at": value_to_json(asset.updated_at),
    }


def character_image_dict(image):
    data = model_dict(image)
    if data:
        data["image_id"] = str(image.image_id)
        data["character_id"] = str(image.character_id)
        data["asset_id"] = str(image.asset_id) if image.asset_id else None
        data["image_url"] = (
            _character_asset_url(image.asset)
            or signed_url_for_asset(
                storage_key=image.storage_path,
                legacy_url=image.image_url,
            )
        )
        data["created_at"] = value_to_json(image.created_at)
        data["updated_at"] = value_to_json(image.updated_at)
    return data


def variant_dict(variant):
    data = model_dict(variant)
    if data:
        data["variant_id"] = str(variant.variant_id)
        data["job_id"] = str(variant.job_id)
        data["character_id"] = str(variant.character_id)
        data["asset_id"] = str(variant.asset_id) if variant.asset_id else None
        data["image_url"] = (
            _character_asset_url(variant.asset)
            or signed_url_for_asset(
                storage_key=None,
                legacy_url=variant.image_url,
            )
        )
        data["created_at"] = value_to_json(variant.created_at)
        data["applied_at"] = value_to_json(variant.applied_at)
    return data


def revision_dict(revision):
    data = model_dict(revision)
    if data:
        data["revision_id"] = str(revision.revision_id)
        data["character_id"] = str(revision.character_id)
        data["project_id"] = revision.project_id
        data["user_id"] = revision.user_id
        data["source_variant_id"] = str(revision.source_variant_id) if revision.source_variant_id else None
        data["source_job_id"] = str(revision.source_job_id) if revision.source_job_id else None
        data["reference_image_id"] = str(revision.reference_image_id) if revision.reference_image_id else None
        data["created_at"] = value_to_json(revision.created_at)
    return data


def character_dict(character, include_related=False):
    data = model_dict(character)
    data.pop("creation_idempotency_key", None)
    data.pop("creation_request_hash", None)
    data["character_id"] = str(character.character_id)
    data["project_id"] = character.project_id
    data["user_id"] = character.user_id
    data["locked_by_id"] = character.locked_by_id
    data["active_appearance_id"] = str(character.active_appearance_id) if character.active_appearance_id else None
    data["active_outfit_id"] = str(character.active_outfit_id) if character.active_outfit_id else None
    data["active_version_id"] = str(character.active_version_id) if character.active_version_id else None
    data["current_revision_id"] = str(character.current_revision_id) if character.current_revision_id else None
    data["canonical_reference_image_id"] = (
        str(character.canonical_reference_image_id) if character.canonical_reference_image_id else None
    )
    data["created_at"] = value_to_json(character.created_at)
    data["updated_at"] = value_to_json(character.updated_at)
    data["locked_at"] = value_to_json(character.locked_at)
    active_images = character.images.filter(is_active=True).select_related("asset")
    data["images"] = {
        image.image_type: character_image_dict(image)
        for image in active_images
    }
    data["clothing_references"] = [
        asset_dict(a)
        for a in character.assets.filter(asset_type="clothing_reference").order_by("created_at")
    ]
    if include_related:
        data["appearance"] = appearance_dict(character.active_appearance)
        data["outfits"] = [outfit_dict(outfit) for outfit in character.outfits.filter(archived_at__isnull=True)]
        data["references"] = [asset_dict(asset) for asset in character.assets.order_by("-created_at")[:20]]
        data["active_version"] = model_dict(character.active_version)
        data["current_revision"] = revision_dict(character.current_revision)
    return data


def references_payload(character, readiness):
    """Build the full GET /references/ response.

    Always returns all 9 reference rows in REFERENCE_UI_ORDER so the UI grid
    layout is stable regardless of which assets exist yet.
    """
    from w_craft_back.character_studio.services.asset_service import (
        REFERENCE_UI_ORDER,
        REFERENCE_UI_TO_ASSET_TYPE,
    )

    latest = readiness["latest_ready_by_type"]

    # The "live" view has to surface in-progress generations and failures, not
    # only ready rows. Pick the most recent row per asset_type that the user
    # cares about: ready (latest version), or generating, or failed.
    rows = []
    primary_id = None
    from w_craft_back.character_studio.models import CharacterAsset, CharacterAssetStatus

    missing_types = [
        REFERENCE_UI_TO_ASSET_TYPE[ui]
        for ui in REFERENCE_UI_ORDER
        if latest.get(REFERENCE_UI_TO_ASSET_TYPE[ui]) is None
    ]
    fallback_by_type: dict = {}
    if missing_types:
        # One query instead of nine: pull every non-ready row for the missing
        # types and keep the newest per asset_type in Python.
        qs = (
            CharacterAsset.objects
            .filter(character=character, asset_type__in=missing_types)
            .exclude(status=CharacterAssetStatus.READY)
            .order_by("asset_type", "-created_at")
        )
        for asset in qs:
            fallback_by_type.setdefault(asset.asset_type, asset)

    for ui_type in REFERENCE_UI_ORDER:
        asset_type = REFERENCE_UI_TO_ASSET_TYPE[ui_type]
        chosen = latest.get(asset_type) or fallback_by_type.get(asset_type)
        rows.append(reference_dict(chosen, ui_type))
        if chosen and chosen.is_primary:
            primary_id = str(chosen.asset_id)

    return {
        "character": {
            "character_id": str(character.character_id),
            "name": character.name,
            "identity_locked": bool(character.identity_locked),
            "status": character.status,
        },
        "references": rows,
        "primary_reference_id": primary_id,
        "checklist": readiness["checklist"],
        "can_proceed_to_3d": readiness["can_proceed"],
        "proceed_blockers": readiness["blockers"],
    }


def job_dict(job, include_variants=True):
    data = model_dict(job)
    for internal_field in (
        "lease_token",
        "request_hash",
        "idempotency_key",
        "compiled_prompt",
        "negative_prompt",
        "edit_instruction",
        "compiled_metadata",
        "provider_started_at",
    ):
        data.pop(internal_field, None)
    data["job_id"] = str(job.job_id)
    data["character_id"] = str(job.character_id)
    data["project_id"] = job.project_id
    # ``user`` remains legacy character-creator attribution; ``actor`` is the
    # collaborator who authorized and started this concrete paid operation.
    data["retry_of"] = str(job.retry_of_id) if job.retry_of_id else None
    data["user_id"] = job.user_id
    data["creator_id"] = job.user_id
    data["actor_id"] = job.actor_id
    data["created_at"] = value_to_json(job.created_at)
    data["started_at"] = value_to_json(job.started_at)
    data["completed_at"] = value_to_json(job.completed_at)
    data["cancellation_requested_at"] = value_to_json(job.cancellation_requested_at)
    data["failed_at"] = value_to_json(job.failed_at)
    data["lease_expires_at"] = value_to_json(job.lease_expires_at)
    data["heartbeat_at"] = value_to_json(job.heartbeat_at)
    if include_variants:
        data["variants"] = [variant_dict(variant) for variant in job.variants.order_by("variant_index")]
    return data


def job_history_dict(job):
    data = job_dict(job, include_variants=False)
    for sensitive_field in ("request_payload", "preserve_options"):
        data.pop(sensitive_field, None)
    return data
