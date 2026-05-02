import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from rest_framework.decorators import api_view

logger = logging.getLogger(__name__)

from w_craft_back.character_studio.models import CharacterAsset, CharacterAssetType, CharacterOutfit, CharacterRevision
from w_craft_back.character_studio.repositories.repositories import OutfitRepository
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.errors import CharacterStudioError, NotFoundError, PermissionDeniedError
from w_craft_back.character_studio.services.generation_service import CharacterGenerationService
from w_craft_back.character_studio.services.permissions import get_owned_project, get_user_from_request
from w_craft_back.character_studio.services.revision_service import CharacterRevisionService
from w_craft_back.character_studio.services.serialization import (
    asset_dict,
    character_dict,
    job_dict,
    outfit_dict,
    revision_dict,
)


def payload(request):
    data = request.data or {}
    return data.get("data", data)


def ok(data=None, status=200):
    response_data = {} if data is None else data
    return JsonResponse(response_data, status=status, safe=isinstance(response_data, dict))


def handle_errors(func):
    def wrapped(request, *args, **kwargs):
        try:
            return func(request, *args, **kwargs)
        except CharacterStudioError as exc:
            return JsonResponse(
                {"error_code": exc.error_code, "message": exc.message},
                status=exc.status_code,
            )
        except Exception as exc:
            logger.exception("Unhandled error in %s", func.__name__)
            return JsonResponse(
                {"error_code": "INTERNAL_ERROR", "message": str(exc)},
                status=500,
            )

    return wrapped


@api_view(["GET", "POST"])
@handle_errors
def characters_collection(request, project_id):
    user = get_user_from_request(request)
    project = get_owned_project(user, project_id)
    service = CharacterService()
    if request.method == "GET":
        filters = {
            "role": request.GET.get("role"),
            "search": request.GET.get("search"),
        }
        return ok(service.list_project_characters(user, project.id, filters), status=200)
    logger.info("create_character start: user=%s project_id=%s", user.id, project_id)
    character = service.create_character(user, project, payload(request))
    logger.info("create_character done: character_id=%s project_id=%s", character.character_id, project_id)
    return ok(character_dict(character, include_related=True), status=201)


@api_view(["GET", "PATCH", "DELETE"])
@handle_errors
def character_detail(request, project_id, character_id):
    user = get_user_from_request(request)
    service = CharacterService()
    if request.method == "GET":
        character = service.get_character(user, project_id, character_id)
        return ok(character_dict(character, include_related=True))
    if request.method == "DELETE":
        service.delete_character(user, project_id, character_id)
        return ok(status=204)
    character = service.update_character(user, project_id, character_id, payload(request))
    return ok(character_dict(character, include_related=True))


@api_view(["POST"])
@handle_errors
def generate_initial_variants(request, project_id, character_id):
    user = get_user_from_request(request)
    data = payload(request)
    service = CharacterGenerationService()
    logger.info("generate_initial_variants start: user=%s project_id=%s character_id=%s", user.id, project_id, character_id)
    if data.get("image_types"):
        jobs = service.create_initial_image_set(user, project_id, character_id, data)
        failed = next((job for job in jobs if job.status == "failed"), None)
        primary_job = failed or (jobs[0] if jobs else None)
        status = "failed" if failed else "completed"
        logger.info("generate_initial_variants done: job_id=%s status=%s character_id=%s", str(primary_job.job_id) if primary_job else None, status, character_id)
        return ok(
            {
                "job_id": str(primary_job.job_id) if primary_job else None,
                "status": status,
                "error_code": failed.error_code if failed else "",
                "error_message": failed.error_message if failed else "",
                "jobs": [generation_job_summary(job) for job in jobs],
            }
        )
    job = service.create_initial_variants(user, project_id, character_id, data)
    logger.info("generate_initial_variants done: job_id=%s status=%s character_id=%s", job.job_id, job.status, character_id)
    return ok({"job_id": str(job.job_id), "status": job.status, "error_code": job.error_code, "error_message": job.error_message})


@api_view(["POST"])
@handle_errors
def generate_edit_variants(request, project_id, character_id):
    user = get_user_from_request(request)
    job = CharacterGenerationService().generate_edit_variants(user, project_id, character_id, payload(request))
    return ok({"job_id": str(job.job_id), "status": job.status, "error_code": job.error_code, "error_message": job.error_message})


def generation_job_summary(job):
    return {
        "job_id": str(job.job_id),
        "status": job.status,
        "image_type": job.request_payload.get("image_type"),
        "error_code": job.error_code,
        "error_message": job.error_message,
    }


@api_view(["GET"])
@handle_errors
def get_generation_job(request, job_id):
    user = get_user_from_request(request)
    job = CharacterGenerationService().get_generation_job(job_id)
    if job.user_id != user.id:
        raise PermissionDeniedError()
    return ok(job_dict(job))


@api_view(["POST"])
@handle_errors
def apply_variant(request, project_id, character_id):
    user = get_user_from_request(request)
    data = payload(request)
    revision = CharacterService().apply_variant(
        user,
        project_id,
        character_id,
        data.get("variant_id"),
        data,
    )
    return ok(revision_dict(revision), status=201)


@api_view(["POST"])
@handle_errors
def lock_identity(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().lock_identity(user, project_id, character_id, payload(request))
    return ok(character_dict(character, include_related=True))


@api_view(["GET", "POST"])
@handle_errors
def outfits_collection(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_character(user, project_id, character_id)
    if request.method == "GET":
        outfits = character.outfits.filter(archived_at__isnull=True).order_by("-is_default", "name")
        return ok([outfit_dict(outfit) for outfit in outfits])
    data = payload(request)
    outfit = CharacterOutfit.objects.create(
        character=character,
        name=data.get("name") or "Outfit",
        description=data.get("description", ""),
        style=data.get("style", ""),
        color_palette=data.get("color_palette", []),
        layers=data.get("layers", {}),
        is_default=bool(data.get("is_default")),
    )
    if outfit.is_default:
        OutfitRepository().set_default(character, outfit)
        character.active_outfit = outfit
        character.save(update_fields=["active_outfit", "updated_at"])
    return ok(outfit_dict(outfit), status=201)


@api_view(["PATCH", "DELETE"])
@handle_errors
def outfit_detail(request, project_id, character_id, outfit_id):
    user = get_user_from_request(request)
    character = CharacterService().get_character(user, project_id, character_id)
    try:
        outfit = character.outfits.get(outfit_id=outfit_id)
    except CharacterOutfit.DoesNotExist as exc:
        raise NotFoundError("Outfit not found.") from exc
    if request.method == "DELETE":
        outfit.archived_at = timezone.now()
        outfit.save(update_fields=["archived_at", "updated_at"])
        return ok(outfit_dict(outfit))
    data = payload(request)
    for field in ("name", "description", "style", "color_palette", "layers"):
        if field in data:
            setattr(outfit, field, data[field])
    outfit.save()
    return ok(outfit_dict(outfit))


@api_view(["POST"])
@handle_errors
def set_default_outfit(request, project_id, character_id, outfit_id):
    user = get_user_from_request(request)
    character = CharacterService().get_character(user, project_id, character_id)
    try:
        outfit = character.outfits.get(outfit_id=outfit_id)
    except CharacterOutfit.DoesNotExist as exc:
        raise NotFoundError("Outfit not found.") from exc
    OutfitRepository().set_default(character, outfit)
    character.active_outfit = outfit
    character.save(update_fields=["active_outfit", "updated_at"])
    return ok(outfit_dict(outfit))


@api_view(["POST"])
@handle_errors
def generate_outfit_variants(request, project_id, character_id, outfit_id):
    user = get_user_from_request(request)
    data = payload(request)
    data["region"] = "outfit"
    data.setdefault("controls", {})
    data["controls"]["outfit_id"] = str(outfit_id)
    job = CharacterGenerationService().generate_edit_variants(user, project_id, character_id, data)
    return ok({"job_id": str(job.job_id), "status": job.status})


_ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@api_view(["POST"])
@handle_errors
def upload_outfit_reference(request, project_id, character_id, outfit_id):
    user = get_user_from_request(request)
    character = CharacterService().get_character(user, project_id, character_id)
    try:
        outfit = character.outfits.get(outfit_id=outfit_id)
    except CharacterOutfit.DoesNotExist as exc:
        raise NotFoundError("Outfit not found.") from exc

    uploaded = request.FILES.get("file")
    if not uploaded:
        return JsonResponse({"error_code": "NO_FILE", "message": "No file provided."}, status=400)
    if uploaded.content_type not in _ALLOWED_MIME_TYPES:
        return JsonResponse(
            {"error_code": "INVALID_FORMAT", "message": "Only jpg, png and webp are supported."},
            status=400,
        )
    if uploaded.size > _MAX_UPLOAD_BYTES:
        return JsonResponse({"error_code": "FILE_TOO_LARGE", "message": "File exceeds 10 MB limit."}, status=400)

    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[uploaded.content_type]
    rel_path = f"character-studio/outfits/{character_id}/{outfit_id}/{uuid.uuid4().hex}.{ext}"
    abs_path = Path(settings.MEDIA_ROOT) / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    with abs_path.open("wb") as fh:
        for chunk in uploaded.chunks():
            fh.write(chunk)

    media_url = getattr(settings, "MEDIA_URL", "/media/")
    if not media_url.endswith("/"):
        media_url += "/"
    image_url = f"{media_url}{rel_path}"

    asset = CharacterAsset.objects.create(
        character=character,
        project=character.project,
        user=user,
        asset_type=CharacterAssetType.OUTFIT_REFERENCE,
        image_url=image_url,
        storage_path=rel_path,
        mime_type=uploaded.content_type,
        source="upload",
    )

    outfit.reference_image = asset
    outfit.save(update_fields=["reference_image", "updated_at"])
    return ok(asset_dict(asset), status=201)


@api_view(["DELETE"])
@handle_errors
def delete_outfit_reference(request, project_id, character_id, outfit_id):
    user = get_user_from_request(request)
    character = CharacterService().get_character(user, project_id, character_id)
    try:
        outfit = character.outfits.get(outfit_id=outfit_id)
    except CharacterOutfit.DoesNotExist as exc:
        raise NotFoundError("Outfit not found.") from exc

    asset = outfit.reference_image
    outfit.reference_image = None
    outfit.save(update_fields=["reference_image", "updated_at"])
    if asset:
        asset.delete()
    return ok(status=204)


@api_view(["POST"])
@handle_errors
def upload_clothing_reference(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_character(user, project_id, character_id)

    uploaded = request.FILES.get("file")
    if not uploaded:
        return JsonResponse({"error_code": "NO_FILE", "message": "No file provided."}, status=400)
    if uploaded.content_type not in _ALLOWED_MIME_TYPES:
        return JsonResponse(
            {"error_code": "INVALID_FORMAT", "message": "Only jpg, png and webp are supported."},
            status=400,
        )
    if uploaded.size > _MAX_UPLOAD_BYTES:
        return JsonResponse({"error_code": "FILE_TOO_LARGE", "message": "File exceeds 10 MB limit."}, status=400)

    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[uploaded.content_type]
    rel_path = f"character-studio/clothing-refs/{character_id}/{uuid.uuid4().hex}.{ext}"
    abs_path = Path(settings.MEDIA_ROOT) / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    with abs_path.open("wb") as fh:
        for chunk in uploaded.chunks():
            fh.write(chunk)

    media_url = getattr(settings, "MEDIA_URL", "/media/")
    if not media_url.endswith("/"):
        media_url += "/"
    image_url = f"{media_url}{rel_path}"

    asset = CharacterAsset.objects.create(
        character=character,
        project=character.project,
        user=user,
        asset_type=CharacterAssetType.CLOTHING_REFERENCE,
        image_url=image_url,
        storage_path=rel_path,
        mime_type=uploaded.content_type,
        source="upload",
    )
    return ok(asset_dict(asset), status=201)


@api_view(["DELETE"])
@handle_errors
def delete_clothing_reference(request, project_id, character_id, asset_id):
    user = get_user_from_request(request)
    character = CharacterService().get_character(user, project_id, character_id)
    try:
        asset = character.assets.get(asset_id=asset_id, asset_type=CharacterAssetType.CLOTHING_REFERENCE)
    except CharacterAsset.DoesNotExist as exc:
        raise NotFoundError("Clothing reference not found.") from exc
    asset.delete()
    return ok(status=204)


@api_view(["GET"])
@handle_errors
def revisions_collection(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_character(user, project_id, character_id)
    return ok(CharacterRevisionService().list_revisions(character))


@api_view(["POST"])
@handle_errors
def restore_revision(request, project_id, character_id, revision_id):
    user = get_user_from_request(request)
    character = CharacterService().get_character(user, project_id, character_id)
    try:
        revision = CharacterRevision.objects.get(character=character, revision_id=revision_id)
    except CharacterRevision.DoesNotExist as exc:
        raise NotFoundError("Revision not found.") from exc
    new_revision = CharacterRevisionService().restore_revision(character, revision, user)
    return ok(revision_dict(new_revision), status=201)
