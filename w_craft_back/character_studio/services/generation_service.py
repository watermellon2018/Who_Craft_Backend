from django.db import transaction
from django.utils import timezone

from w_craft_back.character_studio.models import (
    CharacterAssetType,
    CharacterGenerationJob,
    GenerationJobStatus,
    GenerationJobType,
)
from w_craft_back.character_studio.repositories.repositories import GenerationJobRepository, VariantRepository
from w_craft_back.character_studio.services.asset_service import CharacterAssetService
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.errors import NotFoundError, ValidationError
from w_craft_back.character_studio.services.prompt_compiler import CharacterPromptCompiler
from w_craft_back.character_studio.services.providers import get_image_provider
from w_craft_back.character_studio.services.safety import CharacterSafetyService
from w_craft_back.character_studio.services.serialization import job_dict


class CharacterGenerationService:
    def __init__(self):
        self.jobs = GenerationJobRepository()
        self.variants = VariantRepository()
        self.assets = CharacterAssetService()
        self.characters = CharacterService()
        self.compiler = CharacterPromptCompiler()
        self.safety = CharacterSafetyService()

    @transaction.atomic
    def create_initial_variants(self, user, project_id, character_id, params):
        character = self.characters.get_character(user, project_id, character_id)
        self.characters._ensure_editable(character)
        variant_count = self._validate_variant_count(params.get("variant_count", 4))
        compiled = self.compiler.compile(
            project_style=params.get("project_style"),
            character=character,
            appearance=character.active_appearance,
            outfit=character.active_outfit,
            region="full_character",
            controls=params,
            text_refinement=params.get("text_refinement", ""),
            preserve=params.get("preserve", {}),
            identity_locked=character.identity_locked,
            reference_images=self._reference_ids(character),
        )
        return self._run_job(character, GenerationJobType.INITIAL_VARIANTS, "full_character", variant_count, params, compiled)

    @transaction.atomic
    def generate_edit_variants(self, user, project_id, character_id, edit_request):
        character = self.characters.get_character(user, project_id, character_id)
        self.characters._ensure_editable(character)
        region = edit_request.get("region")
        if region not in ("face", "hair", "body", "outfit", "style", "full_character"):
            raise ValidationError("region is invalid.")
        if len(edit_request.get("text_refinement", "") or "") > 500:
            raise ValidationError("text_refinement max length is 500.")
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
        )
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
            provider="mock",
        )
        job.status = GenerationJobStatus.PROCESSING
        job.progress = 40
        job.started_at = timezone.now()
        job.save()
        provider = get_image_provider("mock")
        try:
            if job_type == GenerationJobType.INITIAL_VARIANTS:
                results = provider.generate_character_variants(job, compiled, variant_count)
                asset_type = CharacterAssetType.INITIAL_VARIANT
            else:
                results = provider.edit_character_region(job, compiled, variant_count)
                asset_type = CharacterAssetType.EDIT_VARIANT
            for item in results:
                asset = self.assets.save_asset(
                    character,
                    asset_type,
                    image_url=item["image_url"],
                    storage_path=item["storage_path"],
                    width=item["width"],
                    height=item["height"],
                    mime_type=item["mime_type"],
                    source="mock",
                    source_job_id=job.job_id,
                    generation_prompt=item["prompt"],
                    negative_prompt=item["negative_prompt"],
                    model_name=item["model_name"],
                    model_version=item["model_version"],
                    seed=item["seed"],
                    metadata=item["metadata"],
                    safety_status="passed",
                )
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
            job.status = GenerationJobStatus.COMPLETED
            job.progress = 100
            job.model_name = provider.model_name
            job.model_version = provider.model_version
            job.completed_at = timezone.now()
            job.save()
        except Exception as exc:
            job.status = GenerationJobStatus.FAILED
            job.error_code = "GENERATION_FAILED"
            job.error_message = str(exc)
            job.failed_at = timezone.now()
            job.save()
        return job

    def _reference_ids(self, character):
        if character.canonical_reference_image_id:
            return [str(character.canonical_reference_image_id)]
        return []

    def _validate_variant_count(self, count):
        try:
            count_value = int(count or 4)
        except (TypeError, ValueError) as exc:
            raise ValidationError("variant_count must be a number.") from exc
        if count_value < 3 or count_value > 4:
            raise ValidationError("variant_count must be 3 or 4.")
        return count_value

    def serialize_job(self, job):
        return job_dict(job)
