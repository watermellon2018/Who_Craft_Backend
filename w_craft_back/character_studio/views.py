import hashlib
import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes

from w_craft_back.auth.authentication import (
    LegacyMultipartUserKeyAuthentication,
)
from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetType,
    CharacterOutfit,
    CharacterRevision,
    CharacterStatus,
    RevisionChangeType,
)
from w_craft_back.character_studio.repositories.repositories import OutfitRepository
from w_craft_back.character_studio.services.asset_service import CharacterAssetService
from w_craft_back.character_studio.services.upload_validation import (
    UploadValidationError,
    validate_image_upload,
)
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.errors import (
    CharacterStudioError,
    NotFoundError,
    ValidationError,
)
from w_craft_back.character_studio.services.generation_lifecycle import (
    build_generation_preview,
    list_character_jobs,
    request_job_cancellation,
    retry_character_job,
    require_idempotency_key,
)
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.character_studio.services.model3d_autofit_service import (
    MODEL3D_AUTOFIT_VERSION,
    compute_autofit,
)
from w_craft_back.character_studio.services.model3d_service import (
    validate_model3d_params,
)
from w_craft_back.character_studio.services.model3d_reconstruction_service import (
    ensure_reconstruction,
    reconstruction_state,
    retry_reconstruction,
)
from w_craft_back.character_studio.services.permissions import (
    get_editable_project,
    get_generation_project,
    get_user_from_request,
    get_viewable_project,
)
from w_craft_back.character_studio.services.revision_service import (
    CharacterRevisionService,
)
from w_craft_back.movie.project.policy import Action
from w_craft_back.upload_protection import UploadLimitExceeded
from w_craft_back.storage_gateway import (
    StorageGatewayError,
    delete_storage_key,
    store_normalized_image,
)
from w_craft_back.character_studio.services.serialization import (
    asset_dict,
    character_dict,
    job_history_dict,
    job_dict,
    outfit_dict,
    reference_dict,
    references_payload,
    revision_dict,
)

logger = logging.getLogger(__name__)


def payload(request):
    data = request.data or {}
    return data.get("data", data)


def generation_payload(request):
    data = payload(request)
    data = data.copy() if hasattr(data, "copy") else dict(data)
    idempotency_key = require_idempotency_key(
        request.headers.get("Idempotency-Key")
        or request.META.get("HTTP_IDEMPOTENCY_KEY")
        or ""
    )
    data["_idempotency_key"] = idempotency_key
    return data


def ok(data=None, status=200):
    response_data = {} if data is None else data
    return JsonResponse(
        response_data, status=status, safe=isinstance(response_data, dict),
    )


def _get_user_outfit(character, outfit_id):
    """Fetch an outfit belonging to ``character`` or raise ``NotFoundError``."""
    try:
        return character.outfits.get(outfit_id=outfit_id)
    except CharacterOutfit.DoesNotExist as exc:
        raise NotFoundError("Outfit not found.") from exc


def handle_errors(func):
    def wrapped(request, *args, **kwargs):
        try:
            return func(request, *args, **kwargs)
        except CharacterStudioError as exc:
            return JsonResponse(
                {"error_code": exc.error_code, "message": exc.message},
                status=exc.status_code,
            )
        except UploadLimitExceeded:
            raise
        except Exception:
            # Never echo raw exception text to clients — internal paths, SQL
            # fragments, etc. leak. The traceback is captured in logs instead.
            logger.exception("Unhandled error in %s", func.__name__)
            return JsonResponse(
                {"error_code": "INTERNAL_ERROR", "message": "internal_error"},
                status=500,
            )

    return wrapped


@api_view(["GET", "POST"])
@handle_errors
def characters_collection(request, project_id):
    user = get_user_from_request(request)
    service = CharacterService()
    if request.method == "GET":
        project = get_viewable_project(user, project_id)
        filters = {
            "role": request.GET.get("role"),
            "search": request.GET.get("search"),
        }
        return ok(
            service.list_project_characters(user, project.id, filters), status=200,
        )
    # Creating a character requires content-edit permission (viewers rejected).
    project = get_editable_project(user, project_id)
    logger.info("create_character start: project_id=%s", project_id)
    character = service.create_character(user, project, payload(request))
    logger.info(
        "create_character done: character_id=%s project_id=%s",
        character.character_id, project_id,
    )
    return ok(character_dict(character, include_related=True), status=201)


def _form_value(request, key, default=""):
    """Read a multipart form text field. Falls back to request.data."""
    if key in request.POST:
        return request.POST.get(key)
    return request.data.get(key, default) if request.data else default


def _form_int(request, key):
    raw = _form_value(request, key, "")
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _form_bool(request, key, default=False):
    raw = _form_value(request, key, None)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _reference_creation_request_hash(uploaded, character, generation):
    digest = hashlib.sha256()
    uploaded.seek(0)
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)
    canonical = json.dumps(
        {
            "version": 1,
            "character": character,
            "generation": generation,
            "file_sha256": digest.hexdigest(),
            "content_type": getattr(uploaded, "content_type", ""),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@api_view(["POST"])
@authentication_classes([LegacyMultipartUserKeyAuthentication])
@handle_errors
def create_character_from_reference(request, project_id):
    user = get_user_from_request(request)
    project = get_editable_project(user, project_id)
    get_generation_project(user, project_id)
    uploaded = request.FILES.get("reference_image")
    if not uploaded:
        raise ValidationError("reference_image is required.")
    try:
        validate_image_upload(uploaded)
    except UploadValidationError as exc:
        raise ValidationError(exc.message) from exc

    char_payload = {
        "name": _form_value(request, "name", ""),
        "character_type": _form_value(request, "character_type")
        or _form_value(request, "entity_type")
        or None,
        "role": _form_value(request, "role") or "",
        "age": _form_int(request, "age"),
        "lifecycle_stage": _form_value(request, "lifecycle_stage") or "",
        "gender": _form_value(request, "gender") or "",
        "visual_style": _form_value(request, "visual_style")
        or _form_value(request, "style") or "",
        "short_description": _form_value(request, "description")
        or _form_value(request, "short_description") or "",
    }

    idempotency_key = require_idempotency_key(
        request.headers.get("Idempotency-Key")
        or request.META.get("HTTP_IDEMPOTENCY_KEY")
        or ""
    )
    generation_params = {
        "_idempotency_key": idempotency_key,
        "variant_count": _form_int(request, "variants_count")
        or _form_int(request, "variant_count") or 1,
        "preserve_identity": _form_bool(request, "use_image_as_identity", default=True)
        if "use_image_as_identity" in request.POST
        else _form_bool(request, "preserve_identity", default=True),
        "visual_style": char_payload["visual_style"],
        "text_refinement": _form_value(request, "refinement")
        or _form_value(request, "text_refinement") or "",
    }
    request_hash = (
        _reference_creation_request_hash(
            uploaded,
            char_payload,
            {
                key: value
                for key, value in generation_params.items()
                if key != "_idempotency_key"
            },
        )
        if idempotency_key
        else ""
    )

    logger.info(
        "create_character_from_reference start:"
        " project_id=%s name_len=%d size=%s mime=%s",
        project_id, len(char_payload["name"] or ""), getattr(uploaded, "size", "?"),
        getattr(uploaded, "content_type", "?"),
    )

    character, reference_asset = CharacterService().create_character_from_reference(
        user,
        project,
        char_payload,
        uploaded,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    job = CharacterGenerationService(execute_immediately=False).create_reference_variants(
        user, project_id, character.character_id, reference_asset, generation_params
    )

    logger.info(
        "create_character_from_reference done: character_id=%s job_id=%s job_status=%s",
        character.character_id, job.job_id, job.status,
    )

    if (
        job.status == "failed"
        and job.error_code == "PROVIDER_OUTCOME_UNKNOWN"
    ):
        raise CharacterStudioError(
            message=job.error_message,
            error_code=job.error_code,
            status_code=503,
        )

    # If the generation kickoff failed (provider unavailable, model can't accept
    # image input, etc.), we don't want to leave a half-created character with no
    # variants cluttering the user's character list. Roll back: delete the
    # character (cascades to assets + job rows) and unlink the uploaded file from
    # disk, then return a 400 with the upstream error code/message.
    if job.status == "failed":
        error_code = job.error_code or "REFERENCE_GENERATION_FAILED"
        error_message = (
            job.error_message
            or "Не удалось сгенерировать варианты по референсу."
            " Попробуйте ещё раз позже."
        )
        ref_path = reference_asset.storage_path
        try:
            # Cascades to CharacterAsset (incl. reference), job, variants.
            character.delete()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to roll back character after generation failure"
                " (character_id=%s)",
                character.character_id,
            )
        if ref_path:
            try:
                delete_storage_key(ref_path)
            except (OSError, StorageGatewayError):
                logger.warning("Could not delete reference file %s", ref_path)
        logger.info(
            "create_character_from_reference rolled back: project_id=%s error_code=%s",
            project_id, error_code,
        )
        raise CharacterStudioError(
            message=error_message, error_code=error_code, status_code=400,
        )

    response = {
        "character": character_dict(character, include_related=True),
        "reference": asset_dict(reference_asset),
        "generation_job": job_dict(job),
    }
    return ok(response, status=202)


@api_view(["GET", "PATCH", "DELETE"])
@handle_errors
def character_detail(request, project_id, character_id):
    user = get_user_from_request(request)
    service = CharacterService()
    if request.method == "GET":
        character = service.get_viewable_character(user, project_id, character_id)
        return ok(character_dict(character, include_related=True))
    if request.method == "DELETE":
        service.delete_character(user, project_id, character_id)
        return ok(status=204)
    character = service.update_character(
        user, project_id, character_id, payload(request),
    )
    return ok(character_dict(character, include_related=True))


@api_view(["GET"])
@handle_errors
def generation_preview(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_generation_character(
        user, project_id, character_id
    )
    image_types = [
        value.strip()
        for value in (request.GET.get("image_types") or "").split(",")
        if value.strip()
    ]
    return ok(
        build_generation_preview(
            actor=user, character=character, image_types=image_types
        )
    )


@api_view(["POST"])
@handle_errors
def generate_initial_variants(request, project_id, character_id):
    user = get_user_from_request(request)
    data = generation_payload(request)
    service = CharacterGenerationService(execute_immediately=False)
    logger.info(
        "generate_initial_variants start: project_id=%s character_id=%s",
        project_id, character_id,
    )
    if data.get("image_types"):
        jobs = service.create_initial_image_set(user, project_id, character_id, data)
        failed = next((job for job in jobs if job.status == "failed"), None)
        primary_job = failed or (jobs[0] if jobs else None)
        status = failed.status if failed else primary_job.status
        logger.info(
            "generate_initial_variants done: job_id=%s status=%s character_id=%s",
            str(primary_job.job_id) if primary_job else None, status, character_id,
        )
        return ok(
            {
                "job_id": str(primary_job.job_id) if primary_job else None,
                "status": status,
                "error_code": failed.error_code if failed else "",
                "error_message": failed.error_message if failed else "",
                "jobs": [generation_job_summary(job) for job in jobs],
            },
            status=202,
        )
    job = service.create_initial_variants(user, project_id, character_id, data)
    logger.info(
        "generate_initial_variants done: job_id=%s status=%s character_id=%s",
        job.job_id, job.status, character_id,
    )
    return ok({
        "job_id": str(job.job_id),
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
    }, status=202)


@api_view(["POST"])
@handle_errors
def generate_edit_variants(request, project_id, character_id):
    user = get_user_from_request(request)
    job = CharacterGenerationService(execute_immediately=False).generate_edit_variants(
        user, project_id, character_id, generation_payload(request),
    )
    deps = CharacterGenerationService.dependent_image_types(
        job.request_payload.get("image_type"),
    )
    return ok({
        "job_id": str(job.job_id),
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "dependent_image_types": [str(t) for t in deps],
    }, status=202)


@api_view(["POST"])
@handle_errors
def zone_edit(request, project_id, character_id):
    user = get_user_from_request(request)
    primary_job, secondary_jobs = CharacterGenerationService(execute_immediately=False).generate_zone_edit(
        user, project_id, character_id, generation_payload(request)
    )
    deps = CharacterGenerationService.dependent_image_types(
        primary_job.request_payload.get("image_type")
    )
    return ok({
        "job_id": str(primary_job.job_id),
        "status": primary_job.status,
        "error_code": primary_job.error_code,
        "error_message": primary_job.error_message,
        "dependent_image_types": [str(t) for t in deps],
        "secondary_job_ids": {
            str(job.request_payload.get("image_type")): str(job.job_id)
            for job in secondary_jobs
        },
    }, status=202)


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
    job = CharacterGenerationService().get_generation_job(user, job_id)
    return ok(job_dict(job))


@api_view(["GET"])
@handle_errors
def generation_job_history(request, project_id, character_id):
    user = get_user_from_request(request)
    try:
        limit = int(request.GET.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    jobs = list_character_jobs(
        actor=user,
        project_id=project_id,
        character_id=character_id,
        limit=limit,
    )
    return ok({"jobs": [job_history_dict(job) for job in jobs]})


@api_view(["POST"])
@handle_errors
def retry_generation_job(request, job_id):
    user = get_user_from_request(request)
    job = retry_character_job(actor=user, job_id=job_id)
    return ok({"job_id": str(job.job_id), "status": job.status}, status=202)


@api_view(["POST"])
@handle_errors
def request_generation_job_cancellation(request, job_id):
    user = get_user_from_request(request)
    job = request_job_cancellation(actor=user, job_id=job_id)
    return ok(job_dict(job, include_variants=False), status=202)


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
    character = CharacterService().lock_identity(
        user, project_id, character_id, payload(request),
    )
    return ok(character_dict(character, include_related=True))


@api_view(["GET", "POST"])
@handle_errors
def outfits_collection(request, project_id, character_id):
    user = get_user_from_request(request)
    service = CharacterService()
    if request.method == "GET":
        character = service.get_viewable_character(user, project_id, character_id)
        outfits = character.outfits.filter(archived_at__isnull=True).order_by(
            "-is_default", "name",
        )
        return ok([outfit_dict(outfit) for outfit in outfits])
    character = service.get_editable_character(user, project_id, character_id)
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
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    outfit = _get_user_outfit(character, outfit_id)
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
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    outfit = _get_user_outfit(character, outfit_id)
    OutfitRepository().set_default(character, outfit)
    character.active_outfit = outfit
    character.save(update_fields=["active_outfit", "updated_at"])
    return ok(outfit_dict(outfit))


@api_view(["POST"])
@handle_errors
def generate_outfit_variants(request, project_id, character_id, outfit_id):
    user = get_user_from_request(request)
    data = generation_payload(request)
    data["region"] = "outfit"
    data.setdefault("controls", {})
    data["controls"]["outfit_id"] = str(outfit_id)
    job = CharacterGenerationService(execute_immediately=False).generate_edit_variants(
        user, project_id, character_id, data,
    )
    return ok({"job_id": str(job.job_id), "status": job.status}, status=202)


@api_view(["POST"])
@handle_errors
def upload_outfit_reference(request, project_id, character_id, outfit_id):
    user = get_user_from_request(request)
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    outfit = _get_user_outfit(character, outfit_id)
    uploaded = request.FILES.get("file")
    try:
        validate_image_upload(uploaded)
        stored = store_normalized_image(
            uploaded._storage_gateway_normalized,
            namespace=f"character-studio/outfits/{character_id}/{outfit_id}",
        )
    except UploadValidationError as exc:
        return JsonResponse(
            {"error_code": exc.error_code, "message": exc.message},
            status=exc.status,
        )
    try:
        with transaction.atomic():
            asset = CharacterAsset.objects.create(
                character=character,
                project=character.project,
                user=user,
                asset_type=CharacterAssetType.OUTFIT_REFERENCE,
                image_url="",
                storage_path=stored.storage_key,
                width=stored.width,
                height=stored.height,
                mime_type=stored.mime_type,
                source="upload",
                metadata={
                    "sha256": stored.sha256,
                    "size_bytes": stored.size_bytes,
                },
            )
            outfit.reference_image = asset
            outfit.save(update_fields=["reference_image", "updated_at"])
    except Exception:
        delete_storage_key(stored.storage_key)
        raise
    return ok(asset_dict(asset), status=201)


@api_view(["DELETE"])
@handle_errors
def delete_outfit_reference(request, project_id, character_id, outfit_id):
    user = get_user_from_request(request)
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    outfit = _get_user_outfit(character, outfit_id)

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
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    uploaded = request.FILES.get("file")
    try:
        validate_image_upload(uploaded)
        stored = store_normalized_image(
            uploaded._storage_gateway_normalized,
            namespace=f"character-studio/clothing-refs/{character_id}",
        )
    except UploadValidationError as exc:
        return JsonResponse(
            {"error_code": exc.error_code, "message": exc.message},
            status=exc.status,
        )
    try:
        asset = CharacterAsset.objects.create(
            character=character,
            project=character.project,
            user=user,
            asset_type=CharacterAssetType.CLOTHING_REFERENCE,
            image_url="",
            storage_path=stored.storage_key,
            width=stored.width,
            height=stored.height,
            mime_type=stored.mime_type,
            source="upload",
            metadata={
                "sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
            },
        )
    except Exception:
        delete_storage_key(stored.storage_key)
        raise
    return ok(asset_dict(asset), status=201)


@api_view(["DELETE"])
@handle_errors
def delete_clothing_reference(request, project_id, character_id, asset_id):
    user = get_user_from_request(request)
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    try:
        asset = character.assets.get(
            asset_id=asset_id, asset_type=CharacterAssetType.CLOTHING_REFERENCE,
        )
    except CharacterAsset.DoesNotExist as exc:
        raise NotFoundError("Clothing reference not found.") from exc
    asset.delete()
    return ok(status=204)


@api_view(["GET"])
@handle_errors
def revisions_collection(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_viewable_character(
        user, project_id, character_id,
    )
    return ok(CharacterRevisionService().list_revisions(character))


@api_view(["POST"])
@handle_errors
def restore_revision(request, project_id, character_id, revision_id):
    user = get_user_from_request(request)
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    try:
        revision = CharacterRevision.objects.get(
            character=character, revision_id=revision_id,
        )
    except CharacterRevision.DoesNotExist as exc:
        raise NotFoundError("Revision not found.") from exc
    new_revision = CharacterRevisionService().restore_revision(
        user,
        Action.EDIT_CONTENT,
        character,
        revision,
    )
    return ok(revision_dict(new_revision), status=201)


# ----------------------------------------------------------------------------
# References stage (read board / generate / correct / upload / make-primary /
# readiness / checklist / proceed-to-3D)
# ----------------------------------------------------------------------------


def _readiness_for(character):
    return CharacterAssetService().compute_readiness(character)


@api_view(["GET"])
@handle_errors
def references_collection(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_viewable_character(
        user, project_id, character_id,
    )
    readiness = _readiness_for(character)
    return ok(references_payload(character, readiness))


@api_view(["POST"])
@handle_errors
def references_generate(request, project_id, character_id):
    user = get_user_from_request(request)
    data = generation_payload(request)
    job = CharacterGenerationService(execute_immediately=False).generate_reference(
        user, project_id, character_id, data,
    )
    # Refresh state so the client gets the updated row immediately (the asset
    # is created by _run_job before the request returns when using the mock
    # provider; for real providers it may still be GENERATING).
    character = CharacterService().get_viewable_character(
        user, project_id, character_id,
    )
    readiness = _readiness_for(character)
    return ok({
        "job_id": str(job.job_id),
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "references": references_payload(character, readiness),
    }, status=202)


@api_view(["POST"])
@handle_errors
def references_generate_missing(request, project_id, character_id):
    """Batch-trigger generation of all missing required references.

    Idempotent: re-calling with the same payload returns
    `skipped` for any reference_type that is already ready or generating —
    no duplicate jobs are created.
    """
    user = get_user_from_request(request)
    data = generation_payload(request)
    result = CharacterGenerationService(execute_immediately=False).generate_missing_references(
        user, project_id, character_id, data,
    )
    character = CharacterService().get_viewable_character(
        user, project_id, character_id,
    )
    readiness = _readiness_for(character)
    return ok({
        "created_jobs": result["created_jobs"],
        "skipped": result["skipped"],
        "references": references_payload(character, readiness),
    }, status=202)


@api_view(["POST"])
@handle_errors
def references_correct(request, project_id, character_id, reference_id):
    user = get_user_from_request(request)
    data = generation_payload(request)
    job = CharacterGenerationService(execute_immediately=False).correct_reference(
        user, project_id, character_id, reference_id, data,
    )
    character = CharacterService().get_viewable_character(
        user, project_id, character_id,
    )
    readiness = _readiness_for(character)
    return ok({
        "job_id": str(job.job_id),
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "references": references_payload(character, readiness),
    }, status=202)


@api_view(["POST"])
@authentication_classes([LegacyMultipartUserKeyAuthentication])
@handle_errors
def references_upload(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    ui_type = request.POST.get("reference_type") or request.data.get("reference_type")
    replace_raw = (
        request.POST.get("replace_current")
        or request.data.get("replace_current")
        or ""
    )
    replace = replace_raw.lower() in ("1", "true", "yes")
    uploaded = request.FILES.get("file")
    asset = CharacterAssetService().upload_reference(
        character, user, ui_type, uploaded, replace_current=replace,
    )
    return ok(reference_dict(asset, ui_type), status=201)


@api_view(["POST"])
@handle_errors
def references_make_primary(request, project_id, character_id, reference_id):
    user = get_user_from_request(request)
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    CharacterAssetService().make_primary_reference(character, reference_id)
    readiness = _readiness_for(character)
    return ok(references_payload(character, readiness))


@api_view(["GET"])
@handle_errors
def references_readiness(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_viewable_character(
        user, project_id, character_id,
    )
    readiness = _readiness_for(character)
    return ok({
        "can_proceed": readiness["can_proceed"],
        "required_ready": readiness["can_proceed"],
        "blockers": readiness["blockers"],
        "checklist": readiness["checklist"],
    })


_USER_CHECKLIST_KEYS = (
    "appearance_stable", "face_matches_base", "outfit_readable", "suitable_for_3d",
)


@api_view(["PATCH"])
@handle_errors
def references_checklist(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )
    data = payload(request)
    state = dict(character.references_state or {})
    for key in _USER_CHECKLIST_KEYS:
        if key in data:
            state[key] = bool(data[key])
    character.references_state = state
    character.save(update_fields=["references_state", "updated_at"])
    readiness = _readiness_for(character)
    return ok({
        "checklist": readiness["checklist"],
        "can_proceed": readiness["can_proceed"],
        "blockers": readiness["blockers"],
    })


@api_view(["POST"])
@handle_errors
def references_proceed_to_3d(request, project_id, character_id):
    user = get_user_from_request(request)
    character = CharacterService().get_generation_character(
        user, project_id, character_id,
    )
    readiness = _readiness_for(character)
    if not readiness["can_proceed"]:
        return JsonResponse(
            {
                "can_proceed": False,
                "error_code": "REFERENCES_NOT_READY",
                "message": "Required references are missing.",
                "blockers": readiness["blockers"],
                "checklist": readiness["checklist"],
            },
            status=400,
        )
    # Snapshot the locked-in set so the future 3D step has a stable list to
    # consume. Recorded as a revision; CharacterRevision is the existing audit
    # log so we don't introduce a new table.
    locked_ids = [
        str(asset.asset_id)
        for asset in readiness["latest_ready_by_type"].values()
    ]
    character.status = CharacterStatus.REFERENCES_LOCKED
    character.save(update_fields=["status", "updated_at"])
    CharacterRevisionService().create_revision(
        user,
        Action.EDIT_CONTENT,
        character,
        RevisionChangeType.VERSION_CREATE,
        changed_region="full_character",
        change_summary="references_locked",
        snapshot={"references": locked_ids, "stage": "references_locked"},
    )
    reconstruction = ensure_reconstruction(character, actor=user)
    return ok({
        "can_proceed": True,
        "next_stage": "3d_model",
        "next_url": f"/project/{project_id}/characters/{character_id}/3d-model",
        "locked_reference_ids": locked_ids,
        "reconstruction": reconstruction,
        "job_id": reconstruction.get("job_id"),
    }, status=202)


# ----------------------------------------------------------------------------
# 3D model stage — parametric editor state
# ----------------------------------------------------------------------------


@api_view(["GET", "PUT"])
@handle_errors
def model3d_state(request, project_id, character_id):
    """Read or replace the character's 3D editor parameters.

    GET returns ``{"params": {...}, "updated_at": ...}``. PUT replaces the
    whole document (the editor always saves its complete state) after
    structural validation/clamping and records the change in the existing
    revision audit log.
    """
    user = get_user_from_request(request)
    service = CharacterService()
    if request.method == "GET":
        character = service.get_viewable_character(user, project_id, character_id)
        # `autofit_done` lets the editor decide on open: seed from references
        # the first time, load the saved state every time after.
        return ok({
            "params": character.model3d_params or {},
            "reconstruction": reconstruction_state(character, ensure=True),
            "autofit_done": character.model3d_autofit_done,
            "autofit_version": character.model3d_autofit_version,
            "updated_at": character.updated_at.isoformat(),
        })

    character = service.get_editable_character(user, project_id, character_id)
    data = payload(request)
    cleaned = validate_model3d_params(data.get("params"))
    character.model3d_params = cleaned
    character.save(update_fields=["model3d_params", "updated_at"])
    CharacterRevisionService().create_revision(
        user,
        Action.EDIT_CONTENT,
        character,
        RevisionChangeType.MANUAL_UPDATE,
        changed_region="full_character",
        change_summary="model3d_updated",
        snapshot={"model3d_params": cleaned, "stage": "3d_model"},
    )
    return ok({
        "params": cleaned,
        "updated_at": character.updated_at.isoformat(),
    })


@api_view(["POST"])
@handle_errors
def model3d_reconstruction_retry(request, project_id, character_id):
    """Retry a failed personalized GLB reconstruction for locked references."""
    user = get_user_from_request(request)
    character = CharacterService().get_generation_character(
        user, project_id, character_id,
    )
    if character.status != CharacterStatus.REFERENCES_LOCKED:
        raise ValidationError("References must be locked before 3D reconstruction.")
    return ok(
        {"reconstruction": retry_reconstruction(character, actor=user)},
        status=202,
    )


@api_view(["POST"])
@handle_errors
def model3d_autofit(request, project_id, character_id):
    """Seed the 3D editor parameters from the reference images.

    Runs automatically the first time the editor opens (the user-facing
    "fit to references" button was removed). Extracts colors and — when
    mediapipe is available — facial proportions, persists them as the
    character's ``model3d_params`` and flips ``model3d_autofit_done`` so it
    never overwrites later manual edits, even if the user resets to
    defaults. Idempotent: re-running after the flag is set is a no-op that
    returns the stored params.

    POST (not GET) because the image analysis is expensive enough that it
    must not run on speculative prefetches.
    """
    user = get_user_from_request(request)
    character = CharacterService().get_editable_character(
        user, project_id, character_id,
    )

    if character.model3d_autofit_done and (
        character.model3d_autofit_version >= MODEL3D_AUTOFIT_VERSION
    ):
        return ok({
            "params": character.model3d_params or {},
            "autofit_done": True,
            "autofit_version": character.model3d_autofit_version,
            "warnings": ["already_autofitted"],
            "sources": {},
        })

    result = compute_autofit(character)
    if character.model3d_autofit_done:
        # Upgrade a legacy sparse fit. Preserve non-default values as manual
        # edits; refresh untouched defaults with the newly active face controls.
        upgraded = result["params"]
        legacy_face_defaults = {
            "face_shape": {"shape": "oval", "cheekbones": 0.0},
            "eyes": {
                "eyeSize": 0.0, "eyeDistance": 0.0, "eyeTilt": 0.0,
                "eyeColor": "#3a6ca8",
            },
            "nose": {
                "noseLength": 0.0, "noseWidth": 0.0, "noseTip": 0.0,
                "bridgeHeight": 0.0,
            },
            "mouth": {
                "mouthWidth": 0.0, "upperLip": 0.0, "lowerLip": 0.0,
                "cornerLift": 0.0,
            },
            "jaw_chin": {
                "jawWidth": 0.0, "chinLength": 0.0, "chinShape": 0.0,
            },
            "skin_color": {"skinTone": "#dac0a3", "skinSaturation": 0.0},
            "hair": {
                "hairStyle": "default", "hairColor": "#1E1A18",
                "hairLength": 0.5, "hairVolume": 0.0, "hairShape": "wavy",
            },
        }
        sampled_color_params = {
            "eyes": "eyeColor",
            "hair": "hairColor",
            "skin_color": "skinTone",
        }
        for zone_id, values in (character.model3d_params or {}).items():
            if (
                character.model3d_autofit_version < MODEL3D_AUTOFIT_VERSION
                and zone_id in legacy_face_defaults
            ):
                # Older fits persisted neutral geometry and sampled colors from
                # less accurate extractors. Upgrade untouched defaults while
                # preserving deliberate numeric edits.
                merged = dict(values)
                for param_id, suggestion in upgraded.get(zone_id, {}).items():
                    default = legacy_face_defaults[zone_id].get(param_id)
                    refresh_sampled_color = (
                        character.model3d_autofit_version >= 3
                        and param_id == sampled_color_params.get(zone_id)
                    )
                    if (
                        refresh_sampled_color
                        or param_id not in values
                        or values[param_id] == default
                    ):
                        merged[param_id] = suggestion
                upgraded[zone_id] = merged
            else:
                upgraded.setdefault(zone_id, {}).update(values)
        result["params"] = validate_model3d_params(upgraded)

    retryable_profile_warnings = {
        "profile_unreadable",
        "profile_landmarks_unavailable",
    }
    profile_needs_retry = (
        result.get("sources", {}).get("profile_pending", False)
        or (
            result.get("sources", {}).get("profile")
            and retryable_profile_warnings.intersection(result.get("warnings", []))
        )
    )
    saved_autofit_version = (
        MODEL3D_AUTOFIT_VERSION - 1
        if profile_needs_retry
        else MODEL3D_AUTOFIT_VERSION
    )

    character.model3d_params = result["params"]
    character.model3d_autofit_done = True
    character.model3d_autofit_version = saved_autofit_version
    character.save(
        update_fields=[
            "model3d_params",
            "model3d_autofit_done",
            "model3d_autofit_version",
            "updated_at",
        ],
    )
    CharacterRevisionService().create_revision(
        user,
        Action.EDIT_CONTENT,
        character,
        RevisionChangeType.MANUAL_UPDATE,
        changed_region="full_character",
        change_summary="model3d_autofit",
        snapshot={"model3d_params": result["params"], "stage": "3d_model"},
    )
    result["autofit_done"] = True
    result["autofit_version"] = saved_autofit_version
    return ok(result)
