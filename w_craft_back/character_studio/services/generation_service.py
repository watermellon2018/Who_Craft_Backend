from django.db import transaction
from django.utils import timezone
import os
import logging

from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
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
    )
    IMAGE_TYPE_TO_REGION = {
        CharacterImageType.PORTRAIT: "face",
        CharacterImageType.FULL_BODY: "body",
        CharacterImageType.SCENE: "style",
        CharacterImageType.REFERENCE_SHEET: "full_character",
        CharacterImageType.THREE_QUARTER: "full_character",
        CharacterImageType.PROFILE: "full_character",
        CharacterImageType.BACK_VIEW: "full_character",
        CharacterImageType.EMOTIONS: "face",
        CharacterImageType.POSES: "body",
        CharacterImageType.OUTFIT_DETAILS: "outfit",
    }
    IMAGE_TYPE_TO_ASSET_TYPE = {
        CharacterImageType.PORTRAIT: CharacterAssetType.PORTRAIT,
        CharacterImageType.FULL_BODY: CharacterAssetType.FULL_BODY,
        CharacterImageType.SCENE: CharacterAssetType.SCENE,
        CharacterImageType.REFERENCE_SHEET: CharacterAssetType.REFERENCE_SHEET,
        CharacterImageType.THREE_QUARTER: CharacterAssetType.THREE_QUARTER,
        CharacterImageType.PROFILE: CharacterAssetType.PROFILE,
        CharacterImageType.BACK_VIEW: CharacterAssetType.BACK_VIEW,
        CharacterImageType.EMOTIONS: CharacterAssetType.EMOTIONS_SHEET,
        CharacterImageType.POSES: CharacterAssetType.POSES_SHEET,
        CharacterImageType.OUTFIT_DETAILS: CharacterAssetType.OUTFIT_DETAILS,
    }
    # Asset_type values that the References stage exposes. The 'character_sheet'
    # alias from the frontend maps onto reference_sheet.
    REFERENCE_ASSET_TYPES = (
        CharacterAssetType.PORTRAIT,
        CharacterAssetType.FULL_BODY,
        CharacterAssetType.THREE_QUARTER,
        CharacterAssetType.PROFILE,
        CharacterAssetType.BACK_VIEW,
        CharacterAssetType.EMOTIONS_SHEET,
        CharacterAssetType.POSES_SHEET,
        CharacterAssetType.OUTFIT_DETAILS,
        CharacterAssetType.REFERENCE_SHEET,
    )
    REFERENCE_TYPE_TO_IMAGE_TYPE = {
        CharacterAssetType.PORTRAIT: CharacterImageType.PORTRAIT,
        CharacterAssetType.FULL_BODY: CharacterImageType.FULL_BODY,
        CharacterAssetType.THREE_QUARTER: CharacterImageType.THREE_QUARTER,
        CharacterAssetType.PROFILE: CharacterImageType.PROFILE,
        CharacterAssetType.BACK_VIEW: CharacterImageType.BACK_VIEW,
        CharacterAssetType.EMOTIONS_SHEET: CharacterImageType.EMOTIONS,
        CharacterAssetType.POSES_SHEET: CharacterImageType.POSES,
        CharacterAssetType.OUTFIT_DETAILS: CharacterImageType.OUTFIT_DETAILS,
        CharacterAssetType.REFERENCE_SHEET: CharacterImageType.REFERENCE_SHEET,
    }
    # Dependent regeneration map: editing one mode forces re-generation of all
    # downstream modes that derive identity/composition from it. Single source
    # of truth — frontend mirrors this and the response of generate-edit-variants
    # exposes it under dependent_image_types.
    EDIT_DEPENDENCIES = {
        CharacterImageType.PORTRAIT: (
            CharacterImageType.PORTRAIT,
            CharacterImageType.FULL_BODY,
            CharacterImageType.SCENE,
        ),
        CharacterImageType.FULL_BODY: (
            CharacterImageType.FULL_BODY,
            CharacterImageType.SCENE,
        ),
        CharacterImageType.SCENE: (
            CharacterImageType.SCENE,
        ),
    }

    @classmethod
    def dependent_image_types(cls, image_type):
        return cls.EDIT_DEPENDENCIES.get(image_type, (image_type,))

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
        outfit_name = getattr(character.active_outfit, "name", None) or "none"
        outfit_desc = getattr(character.active_outfit, "description", None) or "none"
        self.logger.info(
            "create_initial_variants: character_id=%s visual_style=%s variant_count=%d image_type=%s outfit=%r outfit_desc=%r",
            character.character_id, params.get("visual_style"), variant_count, image_type,
            outfit_name, outfit_desc,
        )
        if image_type == CharacterImageType.PORTRAIT:
            # Use strict portrait-only prompt for initial selection variants.
            # Prevents full-body / scene leakage and locks all user-specified attributes.
            compiled = self.compiler.compile_initial_portrait_selection(
                character=character,
                appearance=character.active_appearance,
                outfit=character.active_outfit,
                params=params,
            )
        else:
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
    def generate_zone_edit(self, user, project_id, character_id, payload):
        """Localized rectangular zone edit on portrait/full_body/scene.

        Applies the edit only to the requested asset_type — no cascading
        regeneration of dependent formats. The caller (frontend) decides
        whether to trigger additional regenerations. Selection is stored in
        request_payload + asset.metadata so a future inpainting provider can
        build a pixel mask without changing the pipeline.
        """
        image_type = self._validate_image_type(payload.get("asset_type") or payload.get("image_type"))
        if image_type not in (
            CharacterImageType.PORTRAIT,
            CharacterImageType.FULL_BODY,
            CharacterImageType.SCENE,
        ):
            raise ValidationError("asset_type must be portrait, full_body, or scene.")
        instruction = (payload.get("instruction") or "").strip()
        if not instruction:
            raise ValidationError("instruction must not be empty.")
        if len(instruction) > 500:
            raise ValidationError("instruction max length is 500.")
        selection = self._validate_selection(payload.get("selection"))
        self.safety.validate_user_text(instruction)

        character = self.characters.get_character(user, project_id, character_id)
        region = self.IMAGE_TYPE_TO_REGION[image_type]
        preserve = {"identity": True, "outside_selection": True}
        compiled = self.compiler.compile(
            project_style=payload.get("project_style"),
            character=character,
            appearance=character.active_appearance,
            outfit=character.active_outfit,
            region=region,
            controls={"zone_edit": True, "zone_instruction": instruction},
            text_refinement=instruction,
            preserve=preserve,
            identity_locked=character.identity_locked,
            reference_images=self._reference_ids(character),
            image_type=image_type,
            zone_edit={"selection": selection, "instruction": instruction},
        )
        primary_payload = {
            "image_type": image_type,
            "region": region,
            "edit_type": "zone_edit",
            "instruction": instruction,
            "selection": selection,
            "preserve": preserve,
            "activate_image": True,
            "variant_count": 1,
        }
        job = self._run_job(
            character, GenerationJobType.EDIT_VARIANTS, region, 1, primary_payload, compiled
        )
        return job, []

    def _validate_selection(self, selection):
        if not isinstance(selection, dict):
            raise ValidationError("selection must be an object with x, y, width, height.")
        try:
            x = float(selection.get("x"))
            y = float(selection.get("y"))
            width = float(selection.get("width"))
            height = float(selection.get("height"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("selection x, y, width, height must be numbers.") from exc
        if x < 0 or y < 0:
            raise ValidationError("selection x and y must be >= 0.")
        if width <= 0 or height <= 0:
            raise ValidationError("selection width and height must be > 0.")
        if x + width > 1 or y + height > 1:
            raise ValidationError("selection must be within [0, 1] (x+width and y+height <= 1).")
        return {"x": x, "y": y, "width": width, "height": height}

    @transaction.atomic
    def generate_reference(self, user, project_id, character_id, params):
        """Generate (or regenerate) a single reference view.

        params: {reference_type, correction_prompt?, preserve_identity?}
        reference_type comes from the UI vocabulary (portrait, full_body,
        three_quarter, profile, back_view, emotions, poses, outfit_details,
        character_sheet) and is mapped to the corresponding CharacterImageType.
        """
        from w_craft_back.character_studio.services.asset_service import (
            REFERENCE_UI_TO_ASSET_TYPE,
        )

        ui_type = (params or {}).get("reference_type")
        if ui_type not in REFERENCE_UI_TO_ASSET_TYPE:
            raise ValidationError(f"Unknown reference_type: {ui_type}.")
        asset_type = REFERENCE_UI_TO_ASSET_TYPE[ui_type]
        image_type = self.REFERENCE_TYPE_TO_IMAGE_TYPE[asset_type]

        character = self.characters.get_character(user, project_id, character_id)

        # Conflict guard: another in-flight job for this same image_type would
        # produce racing results and identical asset_type rows. Reject the new
        # request with 409 instead of silently double-generating.
        active_job_exists = CharacterGenerationJob.objects.filter(
            character=character,
            status__in=[GenerationJobStatus.QUEUED, GenerationJobStatus.PROCESSING],
            request_payload__image_type=image_type,
        ).exists()
        if active_job_exists:
            raise ValidationError(
                "Generation already running for this reference_type.",
            )

        correction_prompt = (params.get("correction_prompt") or "").strip()
        preserve_identity = bool(params.get("preserve_identity", True))
        if correction_prompt:
            self.safety.validate_user_text(correction_prompt)
            if len(correction_prompt) > 500:
                raise ValidationError("correction_prompt max length is 500.")

        region = self.IMAGE_TYPE_TO_REGION[image_type]
        compiled = self.compiler.compile(
            project_style=params.get("project_style"),
            character=character,
            appearance=character.active_appearance,
            outfit=character.active_outfit,
            region=region,
            controls=params.get("controls", {}),
            text_refinement=params.get("text_refinement", ""),
            preserve=params.get("preserve", {}),
            identity_locked=character.identity_locked,
            reference_images=self._reference_ids(character),
            image_type=image_type,
            correction_prompt=correction_prompt,
            preserve_identity=preserve_identity,
        )
        request_payload = {
            **params,
            "image_type": image_type,
            "reference_type": ui_type,
            "asset_type": asset_type,
            "activate_image": False,
            "variant_count": 1,
            "correction_prompt": correction_prompt,
            "preserve_identity": preserve_identity,
        }
        return self._run_job(
            character,
            GenerationJobType.INITIAL_VARIANTS,
            region,
            1,
            request_payload,
            compiled,
        )

    def generate_missing_references(self, user, project_id, character_id, params):
        """Idempotent batch trigger.

        For each requested reference_type:
          - already ready latest -> skip (already_ready)
          - generating placeholder/asset OR active job -> skip (already_generating)
          - otherwise: create a generation job through generate_reference()

        Each per-type call runs in its own transaction (generate_reference is
        @transaction.atomic) so a partial failure on one type does not roll back
        successfully created jobs for the other types.
        """
        from w_craft_back.character_studio.services.asset_service import (
            REFERENCE_UI_TO_ASSET_TYPE,
        )

        params = params or {}
        only_missing = bool(params.get("only_missing", True))
        preserve_identity = bool(params.get("preserve_identity", True))
        requested_types = params.get("reference_types") or []
        if not isinstance(requested_types, (list, tuple)) or not requested_types:
            raise ValidationError("reference_types must be a non-empty list.")

        unknown = [rt for rt in requested_types if rt not in REFERENCE_UI_TO_ASSET_TYPE]
        if unknown:
            raise ValidationError(
                f"Unknown reference_type(s): {', '.join(unknown)}.",
            )

        character = self.characters.get_character(user, project_id, character_id)

        latest_ready = self.assets.latest_ready_by_reference_type(character)
        created_jobs = []
        skipped = []

        for ui_type in requested_types:
            asset_type = REFERENCE_UI_TO_ASSET_TYPE[ui_type]
            image_type = self.REFERENCE_TYPE_TO_IMAGE_TYPE[asset_type]

            if only_missing and asset_type in latest_ready:
                skipped.append({"reference_type": ui_type, "reason": "already_ready"})
                continue

            active_job = CharacterGenerationJob.objects.filter(
                character=character,
                status__in=[GenerationJobStatus.QUEUED, GenerationJobStatus.PROCESSING],
                request_payload__image_type=image_type,
            ).exists()
            if active_job:
                skipped.append({"reference_type": ui_type, "reason": "already_generating"})
                continue

            try:
                job = self.generate_reference(
                    user, project_id, character_id,
                    {
                        "reference_type": ui_type,
                        "preserve_identity": preserve_identity,
                    },
                )
                created_jobs.append({"reference_type": ui_type, "job_id": str(job.job_id)})
            except ValidationError as exc:
                # generate_reference raises ValidationError on the same conflict
                # — treat as a soft skip rather than failing the whole batch.
                if "already running" in str(exc).lower():
                    skipped.append({"reference_type": ui_type, "reason": "already_generating"})
                else:
                    raise

        return {"created_jobs": created_jobs, "skipped": skipped}

    @transaction.atomic
    def correct_reference(self, user, project_id, character_id, reference_id, params):
        """Apply a textual correction to an existing reference and create a NEW
        version. The previous version is preserved (status stays ready) so the
        user can revert by simply marking it primary."""
        try:
            reference = CharacterAsset.objects.get(asset_id=reference_id, character_id=character_id)
        except CharacterAsset.DoesNotExist as exc:
            raise NotFoundError("Reference not found.") from exc
        from w_craft_back.character_studio.services.asset_service import (
            ASSET_TYPE_TO_REFERENCE_UI,
        )
        ui_type = ASSET_TYPE_TO_REFERENCE_UI.get(reference.asset_type)
        if ui_type is None:
            raise ValidationError("Asset is not a reference and cannot be corrected.")
        correction_prompt = (params or {}).get("correction_prompt", "").strip()
        if not correction_prompt:
            raise ValidationError("correction_prompt must not be empty.")
        return self.generate_reference(
            user,
            project_id,
            character_id,
            {
                "reference_type": ui_type,
                "correction_prompt": correction_prompt,
                "preserve_identity": params.get("preserve_identity", True),
            },
        )

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
        self.logger.info(
            "_run_job: job_id=%s job_type=%s character_id=%s provider=%s image_type=%s region=%s",
            job.job_id, job_type, character.character_id, provider_name,
            request_payload.get("image_type", "?"), region,
        )
        self.logger.debug("_run_job prompt: %s", compiled["positive_prompt"][:300])
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
            correction_prompt = (request_payload.get("correction_prompt") or "").strip()
            preserve_identity = bool(request_payload.get("preserve_identity", False))
            for item in results:
                metadata = {
                    **(item.get("metadata") or {}),
                    "image_type": image_type,
                    "job_type": job_type,
                    "edit_instruction": compiled.get("edit_instruction", ""),
                }
                edit_type = request_payload.get("edit_type")
                if edit_type:
                    metadata["edit_type"] = edit_type
                if request_payload.get("selection"):
                    metadata["selection"] = request_payload["selection"]
                if request_payload.get("instruction"):
                    metadata["instruction"] = request_payload["instruction"]
                if correction_prompt:
                    metadata["correction_prompt"] = correction_prompt
                    metadata["preserve_identity"] = preserve_identity
                asset_kwargs = dict(
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
                if correction_prompt:
                    asset_kwargs["correction_prompt"] = correction_prompt
                asset = self.assets.save_asset(
                    character,
                    asset_type,
                    **asset_kwargs,
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
            self.logger.info("_run_job completed: job_id=%s character_id=%s", job.job_id, character.character_id)
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
