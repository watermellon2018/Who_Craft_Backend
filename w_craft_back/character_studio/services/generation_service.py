from django.db import transaction
from django.utils import timezone
import os
import logging

from w_craft_back.character_studio.models import (
    CharacterAssetType,
    CharacterGenerationJob,
    CharacterImageType,
    GenerationJobStatus,
    GenerationJobType,
)
from w_craft_back.character_studio.repositories.repositories import (
    CharacterImageRepository,
    GenerationJobRepository,
    VariantRepository,
)
from w_craft_back.character_studio.services.asset_service import CharacterAssetService
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.errors import NotFoundError, ValidationError
from w_craft_back.character_studio.services.prompt_compiler import CharacterPromptCompiler
from w_craft_back.character_studio.services.providers import ProviderUserFacingError, get_image_provider
from w_craft_back.character_studio.services.safety import CharacterSafetyService
from w_craft_back.character_studio.services.serialization import job_dict


class CharacterGenerationService:
    INITIAL_IMAGE_TYPES = (
        CharacterImageType.PORTRAIT,
        CharacterImageType.FULL_BODY,
        CharacterImageType.SCENE,
        CharacterImageType.REFERENCE_SHEET,
    )
    IMAGE_TYPE_TO_REGION = {
        CharacterImageType.PORTRAIT: "face",
        CharacterImageType.FULL_BODY: "body",
        CharacterImageType.SCENE: "style",
        CharacterImageType.REFERENCE_SHEET: "full_character",
    }
    IMAGE_TYPE_TO_ASSET_TYPE = {
        CharacterImageType.PORTRAIT: CharacterAssetType.PORTRAIT,
        CharacterImageType.FULL_BODY: CharacterAssetType.FULL_BODY,
        CharacterImageType.SCENE: CharacterAssetType.SCENE,
        CharacterImageType.REFERENCE_SHEET: CharacterAssetType.REFERENCE_SHEET,
    }

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.jobs = GenerationJobRepository()
        self.variants = VariantRepository()
        self.images = CharacterImageRepository()
        self.assets = CharacterAssetService()
        self.characters = CharacterService()
        self.compiler = CharacterPromptCompiler()
        self.safety = CharacterSafetyService()

    @transaction.atomic
    def create_initial_variants(self, user, project_id, character_id, params):
        character = self.characters.get_character(user, project_id, character_id)
        variant_count = self._validate_variant_count(params.get("variant_count", 4))
        image_type = self._validate_image_type(params.get("image_type") or params.get("preview_type") or CharacterImageType.PORTRAIT)
        region = self.IMAGE_TYPE_TO_REGION[image_type]
        compiled = self.compiler.compile(
            project_style=params.get("project_style"),
            character=character,
            appearance=character.active_appearance,
            outfit=character.active_outfit,
            region=region,
            controls=params,
            text_refinement=params.get("text_refinement", ""),
            preserve=params.get("preserve", {}),
            identity_locked=character.identity_locked,
            reference_images=self._reference_ids(character),
            image_type=image_type,
        )
        params = {**params, "image_type": image_type, "activate_image": params.get("activate_image", True)}
        return self._run_job(character, GenerationJobType.INITIAL_VARIANTS, region, variant_count, params, compiled)

    def create_initial_image_set(self, user, project_id, character_id, params):
        image_types = params.get("image_types") or self.INITIAL_IMAGE_TYPES
        image_types = [self._validate_image_type(image_type) for image_type in image_types]
        jobs = []
        for image_type in image_types:
            job = self.create_initial_variants(
                user,
                project_id,
                character_id,
                {
                    **params,
                    "image_type": image_type,
                    "variant_count": params.get("variant_count", 1),
                },
            )
            jobs.append(job)
            if job.status == GenerationJobStatus.FAILED:
                break
        return jobs

    @transaction.atomic
    def generate_edit_variants(self, user, project_id, character_id, edit_request):
        character = self.characters.get_character(user, project_id, character_id)
        region = edit_request.get("region")
        if region not in ("face", "hair", "body", "outfit", "style", "full_character"):
            raise ValidationError("region is invalid.")
        if len(edit_request.get("text_refinement", "") or "") > 500:
            raise ValidationError("text_refinement max length is 500.")
        image_type = self._validate_image_type(edit_request.get("image_type") or CharacterImageType.PORTRAIT)
        self.characters.assert_identity_change_allowed(character, region, edit_request)
        self.safety.validate_user_text(edit_request.get("text_refinement", ""))
        variant_count = self._validate_variant_count(edit_request.get("variant_count", 4))
        preserve = edit_request.get("preserve", {})
        compiled = self.compiler.compile(
            project_style=edit_request.get("project_style"),
            character=character,
            appearance=character.active_appearance,
            outfit=character.active_outfit,
            region=region,
            controls=edit_request.get("controls", {}),
            text_refinement=edit_request.get("text_refinement", ""),
            preserve=preserve,
            identity_locked=character.identity_locked,
            reference_images=self._reference_ids(character),
            image_type=image_type,
        )
        edit_request = {**edit_request, "image_type": image_type, "activate_image": edit_request.get("activate_image", True)}
        return self._run_job(character, GenerationJobType.EDIT_VARIANTS, region, variant_count, edit_request, compiled)

    def get_generation_job(self, job_id):
        try:
            return self.jobs.get(job_id=job_id)
        except CharacterGenerationJob.DoesNotExist as exc:
            raise NotFoundError("Generation job not found.") from exc

    @transaction.atomic
    def cancel_generation_job(self, job_id):
        job = self.get_generation_job(job_id)
        if job.status in (GenerationJobStatus.COMPLETED, GenerationJobStatus.FAILED):
            raise ValidationError("Completed jobs cannot be cancelled.")
        job.status = GenerationJobStatus.CANCELLED
        job.progress = 0
        job.save(update_fields=["status", "progress"])
        return job

    def _run_job(self, character, job_type, region, variant_count, request_payload, compiled):
        self.safety.validate_generated_prompt(compiled["positive_prompt"])
        provider_name = (
            (request_payload or {}).get("provider")
            or os.getenv("CHARACTER_STUDIO_IMAGE_PROVIDER")
            or "mock"
        )
        job = self.jobs.create(
            character=character,
            project=character.project,
            user=character.user,
            job_type=job_type,
            status=GenerationJobStatus.QUEUED,
            region=region,
            variant_count=variant_count,
            request_payload=request_payload,
            compiled_prompt=compiled["positive_prompt"],
            negative_prompt=compiled["negative_prompt"],
            edit_instruction=compiled["edit_instruction"],
            preserve_options=compiled["metadata"].get("preserve", {}),
            provider=provider_name,
        )
        job.status = GenerationJobStatus.PROCESSING
        job.progress = 40
        job.started_at = timezone.now()
        job.save()
        provider = get_image_provider(provider_name)
        try:
            image_type = request_payload.get("image_type") or CharacterImageType.PORTRAIT
            if job_type == GenerationJobType.INITIAL_VARIANTS:
                results = provider.generate_character_variants(job, compiled, variant_count)
                asset_type = self.IMAGE_TYPE_TO_ASSET_TYPE.get(image_type, CharacterAssetType.INITIAL_VARIANT)
            else:
                results = provider.edit_character_region(job, compiled, variant_count)
                asset_type = self.IMAGE_TYPE_TO_ASSET_TYPE.get(image_type, CharacterAssetType.EDIT_VARIANT)
            first_asset = None
            for item in results:
                metadata = {
                    **(item.get("metadata") or {}),
                    "image_type": image_type,
                    "job_type": job_type,
                    "edit_instruction": compiled.get("edit_instruction", ""),
                }
                asset = self.assets.save_asset(
                    character,
                    asset_type,
                    image_url=item["image_url"],
                    storage_path=item["storage_path"],
                    width=item["width"],
                    height=item["height"],
                    mime_type=item["mime_type"],
                    source=provider_name,
                    source_job_id=job.job_id,
                    generation_prompt=item["prompt"],
                    negative_prompt=item["negative_prompt"],
                    model_name=item["model_name"],
                    model_version=item["model_version"],
                    seed=item["seed"],
                    metadata=metadata,
                    safety_status="passed",
                )
                first_asset = first_asset or asset
                variant = self.variants.create(
                    job=job,
                    character=character,
                    asset=asset,
                    variant_index=item["variant_index"],
                    region=region,
                    controls_snapshot=request_payload.get("controls", request_payload),
                    appearance_snapshot=compiled["metadata"],
                    image_url=item["image_url"],
                    prompt=item["prompt"],
                    negative_prompt=item["negative_prompt"],
                    seed=item["seed"],
                    model_name=item["model_name"],
                )
                asset.source_variant_id = variant.variant_id
                asset.save(update_fields=["source_variant_id"])
            if first_asset and request_payload.get("activate_image", True):
                self._activate_image(character, first_asset, image_type, request_payload)
            job.status = GenerationJobStatus.COMPLETED
            job.progress = 100
            job.model_name = provider.model_name
            job.model_version = provider.model_version
            job.completed_at = timezone.now()
            job.save()
        except Exception as exc:
            self.logger.exception("Character generation failed (provider=%s job_id=%s)", provider_name, job.job_id)
            job.status = GenerationJobStatus.FAILED
            job.error_code = getattr(exc, "error_code", "GENERATION_FAILED")
            job.error_message = exc.user_message if isinstance(exc, ProviderUserFacingError) else str(exc)
            job.failed_at = timezone.now()
            job.save()
        return job

    def _reference_ids(self, character):
        active_assets = [
            str(image.asset_id)
            for image in character.images.filter(is_active=True).exclude(asset_id__isnull=True)
        ]
        if character.canonical_reference_image_id:
            return [str(character.canonical_reference_image_id), *active_assets]
        return active_assets

    def _validate_variant_count(self, count):
        try:
            count_value = int(count or 4)
        except (TypeError, ValueError) as exc:
            raise ValidationError("variant_count must be a number.") from exc
        if count_value not in (1, 2, 4):
            raise ValidationError("variant_count must be 1, 2, or 4.")
        return count_value

    def _validate_image_type(self, image_type):
        normalized = {
            "fullBody": CharacterImageType.FULL_BODY,
            "sheet": CharacterImageType.REFERENCE_SHEET,
            "character_sheet": CharacterImageType.REFERENCE_SHEET,
        }.get(image_type, image_type)
        if normalized not in CharacterImageType.values:
            raise ValidationError("image_type is invalid.")
        return normalized

    def _activate_image(self, character, asset, image_type, request_payload):
        return self.images.set_active(
            character,
            image_type,
            asset=asset,
            image_url=asset.image_url,
            storage_path=asset.storage_path,
            prompt=asset.generation_prompt,
            seed=asset.seed,
            generation_params=request_payload,
        )

    def serialize_job(self, job):
        return job_dict(job)
