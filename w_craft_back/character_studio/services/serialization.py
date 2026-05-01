from django.forms.models import model_to_dict
from django.conf import settings


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
    if not value or not isinstance(value, str):
        return value
    if value.startswith(("http://", "https://", "data:")):
        return value
    if not value.startswith("/"):
        return value

    base_url = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        return value
    return f"{base_url}{value}"


def appearance_dict(appearance):
    return model_dict(appearance)


def outfit_dict(outfit):
    return model_dict(outfit)


def asset_dict(asset):
    data = model_dict(asset)
    if data:
        data["image_url"] = public_url(data.get("image_url"))
    return data


def character_image_dict(image):
    data = model_dict(image)
    if data:
        data["image_id"] = str(image.image_id)
        data["character_id"] = str(image.character_id)
        data["asset_id"] = str(image.asset_id) if image.asset_id else None
        data["image_url"] = public_url(data.get("image_url"))
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
        data["image_url"] = public_url(data.get("image_url"))
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
    if include_related:
        data["appearance"] = appearance_dict(character.active_appearance)
        data["outfits"] = [outfit_dict(outfit) for outfit in character.outfits.filter(archived_at__isnull=True)]
        data["references"] = [asset_dict(asset) for asset in character.assets.order_by("-created_at")[:20]]
        data["active_version"] = model_dict(character.active_version)
        data["current_revision"] = revision_dict(character.current_revision)
    return data


def job_dict(job, include_variants=True):
    data = model_dict(job)
    data["job_id"] = str(job.job_id)
    data["character_id"] = str(job.character_id)
    data["project_id"] = job.project_id
    data["user_id"] = job.user_id
    data["created_at"] = value_to_json(job.created_at)
    data["started_at"] = value_to_json(job.started_at)
    data["completed_at"] = value_to_json(job.completed_at)
    data["failed_at"] = value_to_json(job.failed_at)
    if include_variants:
        data["variants"] = [variant_dict(variant) for variant in job.variants.order_by("variant_index")]
    return data
