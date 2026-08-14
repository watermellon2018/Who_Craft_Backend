from django.db import transaction
from django.utils import timezone
import hashlib
import logging
import re
import time

from w_craft_back.character_studio.models import (
    CharacterAsset,
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
from w_craft_back.character_studio.services.errors import (
    IdentityAssetRequiredError,
    NotFoundError,
    ValidationError,
)
from w_craft_back.character_studio.services.generation_lifecycle import (
    JobLease,
    claim_job,
    enqueue_job,
    fail_job,
    heartbeat_job,
    mark_provider_started,
    recover_stale_jobs,
)
from w_craft_back.character_studio.services.prompt_compiler import (
    CharacterPromptCompiler,
)
from w_craft_back.character_studio.services.providers import (
    ProviderUserFacingError,
    get_image_provider,
)
from w_craft_back.character_studio.services.safety import CharacterSafetyService
from w_craft_back.observability import log_context
from w_craft_back.movie.project.policy import Action
from w_craft_back.character_studio.services.serialization import job_dict
from w_craft_back.credits.services import capture_provider_generation


_SAFE_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")
_GENERIC_GENERATION_ERROR = "Generation failed. Try again."


def _generation_failure_details(exc: Exception) -> tuple[str, str]:
    candidate = getattr(exc, "error_code", "GENERATION_FAILED")
    error_code = (
        candidate
        if isinstance(candidate, str) and _SAFE_ERROR_CODE.fullmatch(candidate)
        else "GENERATION_FAILED"
    )
    if isinstance(exc, ProviderUserFacingError):
        return error_code, exc.user_message
    return error_code, _GENERIC_GENERATION_ERROR


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

    def __init__(self, *, execute_immediately=True):
        self.execute_immediately = execute_immediately
        self.logger = logging.getLogger(__name__)
        self.jobs = GenerationJobRepository()
        self.variants = VariantRepository()
        self.images = CharacterImageRepository()
        self.assets = CharacterAssetService()
        self.characters = CharacterService()
        self.compiler = CharacterPromptCompiler()
        self.safety = CharacterSafetyService()

    def create_initial_variants(self, user, project_id, character_id, params):
        character = self.characters.get_generation_character(
            user, project_id, character_id,
        )
        variant_count = self._validate_variant_count(params.get("variant_count", 4))
        image_type = self._validate_image_type(params.get("image_type") or params.get("preview_type") or CharacterImageType.PORTRAIT)
        region = self.IMAGE_TYPE_TO_REGION[image_type]
        self.logger.info(
            "character_generation_requested",
            extra={
                "character_id": character.character_id,
                "variant_count": variant_count,
                "image_type": image_type,
            },
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
        return self._run_job(
            user,
            character,
            GenerationJobType.INITIAL_VARIANTS,
            region,
            variant_count,
            params,
            compiled,
        )

    def create_initial_image_set(self, user, project_id, character_id, params):
        image_types = params.get("image_types") or self.INITIAL_IMAGE_TYPES
        image_types = [self._validate_image_type(image_type) for image_type in image_types]
        jobs = []
        base_idempotency_key = (params or {}).get("_idempotency_key")
        for image_type in image_types:
            job = self.create_initial_variants(
                user,
                project_id,
                character_id,
                {
                    **params,
                    "image_type": image_type,
                    "variant_count": params.get("variant_count", 1),
                    "_idempotency_key": self._scoped_idempotency_key(
                        base_idempotency_key,
                        image_type,
                    ),
                },
            )
            jobs.append(job)
            if job.status == GenerationJobStatus.FAILED:
                break
        return jobs

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

        character = self.characters.get_generation_character(
            user, project_id, character_id,
        )
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
            "image_model": payload.get("image_model"),
            "instruction": instruction,
            "selection": selection,
            "preserve": preserve,
            "activate_image": True,
            "variant_count": 1,
        }
        primary_payload["_idempotency_key"] = (payload or {}).get(
            "_idempotency_key"
        )
        job = self._run_job(
            user,
            character,
            GenerationJobType.EDIT_VARIANTS,
            region,
            1,
            primary_payload,
            compiled,
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

    def generate_reference(self, user, project_id, character_id, params):
        """Generate (or regenerate) a single reference view.

        params: {reference_type, correction_prompt?, preserve_identity?}
        reference_type comes from the UI vocabulary (portrait, full_body,
        three_quarter, profile, back_view, emotions, poses, outfit_details,
        character_sheet) and is mapped to the corresponding CharacterImageType.

        Routing:
            - portrait → text-only pipeline (the portrait IS the identity source)
            - everything else → identity-anchored image-to-image pipeline,
              using ``CharacterService.get_identity_asset()`` as the source.
        """
        from pathlib import Path

        from django.conf import settings

        from w_craft_back.character_studio.services.asset_service import (
            REFERENCE_UI_TO_ASSET_TYPE,
        )
        from w_craft_back.character_studio.services.errors import (
            IdentityAssetRequiredError,
        )

        ui_type = (params or {}).get("reference_type")
        if ui_type not in REFERENCE_UI_TO_ASSET_TYPE:
            raise ValidationError(f"Unknown reference_type: {ui_type}.")
        asset_type = REFERENCE_UI_TO_ASSET_TYPE[ui_type]
        image_type = self.REFERENCE_TYPE_TO_IMAGE_TYPE[asset_type]

        character = self.characters.get_generation_character(
            user, project_id, character_id,
        )

        correction_prompt = (params.get("correction_prompt") or "").strip()
        preserve_identity = bool(params.get("preserve_identity", True))
        if correction_prompt:
            self.safety.validate_user_text(correction_prompt)
            if len(correction_prompt) > 500:
                raise ValidationError("correction_prompt max length is 500.")

        # PORTRAIT is the identity source itself — keep the original text-only
        # pipeline so the user can iterate the canonical portrait look.
        if image_type == CharacterImageType.PORTRAIT:
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
                user,
                character,
                GenerationJobType.INITIAL_VARIANTS,
                region,
                1,
                request_payload,
                compiled,
            )

        # Non-portrait reference: derive from the identity asset (image-to-image).
        identity_asset = self.characters.get_identity_asset(character)
        self.logger.info(
            "generate_reference: character=%s image_type=%s has_identity=%s "
            "identity_asset=%s preserve=%s",
            character.character_id,
            image_type,
            bool(identity_asset),
            str(identity_asset.asset_id) if identity_asset else None,
            preserve_identity,
        )
        if identity_asset is None:
            raise IdentityAssetRequiredError()

        # Load the identity image bytes up front so a missing-file surfaces
        # before we spin up the job.
        abs_path = Path(settings.MEDIA_ROOT) / identity_asset.storage_path
        try:
            reference_bytes = abs_path.read_bytes()
        except OSError as exc:
            raise ValidationError(
                "Identity reference image file is missing on storage."
            ) from exc
        mime_type = identity_asset.mime_type or "image/png"

        compiled = self.compiler.compile_identity_anchored(
            character=character,
            appearance=character.active_appearance,
            outfit=character.active_outfit,
            image_type=image_type,
            params={
                "preserve_identity": preserve_identity,
                "text_refinement": params.get("text_refinement", ""),
                "correction_prompt": correction_prompt,
                "visual_style": params.get("visual_style"),
            },
        )
        request_payload = {
            **params,
            "image_type": image_type,
            "reference_type": ui_type,
            "asset_type": asset_type,
            "variant_count": 1,
            "preserve_identity": preserve_identity,
            "correction_prompt": correction_prompt,
            "reference_asset_id": str(identity_asset.asset_id),
            "source_identity_asset_id": str(identity_asset.asset_id),
            "activate_image": params.get("activate_image", False),
        }
        return self._run_job_with_reference(
            user,
            character,
            request_payload,
            compiled,
            reference_bytes,
            mime_type,
        )

    def generate_missing_references(self, user, project_id, character_id, params):
        """Idempotent batch trigger.

        For each requested reference_type:
          - already ready latest -> skip (already_ready)
          - generating placeholder/asset OR active job -> skip (already_generating)
          - otherwise: create a generation job through generate_reference()

        Each per-type call owns an independent enqueue/provider/finalize
        lifecycle, so a failure on one type does not roll back successfully
        completed jobs for the other types.
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

        character = self.characters.get_generation_character(
            user, project_id, character_id,
        )

        latest_ready = self.assets.latest_ready_by_reference_type(character)
        created_jobs = []
        skipped = []
        base_idempotency_key = params.get("_idempotency_key")

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
                        "image_model": params.get("image_model"),
                        "_idempotency_key": self._scoped_idempotency_key(
                            base_idempotency_key,
                            ui_type,
                        ),
                    },
                )
                created_jobs.append({"reference_type": ui_type, "job_id": str(job.job_id)})
            except IdentityAssetRequiredError:
                # A portrait (or uploaded reference) must finish before
                # identity-anchored angles can be queued. Keep the batch
                # request successful and let the client retry after the
                # identity job completes.
                skipped.append(
                    {"reference_type": ui_type, "reason": "identity_pending"}
                )
            except ValidationError as exc:
                # generate_reference raises ValidationError on the same conflict
                # — treat as a soft skip rather than failing the whole batch.
                if "already running" in str(exc).lower():
                    skipped.append({"reference_type": ui_type, "reason": "already_generating"})
                else:
                    raise

        return {"created_jobs": created_jobs, "skipped": skipped}

    def correct_reference(self, user, project_id, character_id, reference_id, params):
        """Apply a textual correction to an existing reference and create a NEW
        version. The previous version is preserved (status stays ready) so the
        user can revert by simply marking it primary."""
        character = self.characters.get_generation_character(
            user, project_id, character_id,
        )
        try:
            reference = CharacterAsset.objects.get(
                asset_id=reference_id, character=character,
            )
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
                "image_model": params.get("image_model"),
                "_idempotency_key": (params or {}).get("_idempotency_key"),
            },
        )

    def generate_edit_variants(self, user, project_id, character_id, edit_request):
        character = self.characters.get_generation_character(
            user, project_id, character_id,
        )
        region = edit_request.get("region")
        if region not in ("face", "hair", "body", "outfit", "style", "full_character"):
            raise ValidationError("region is invalid.")
        if len(edit_request.get("text_refinement", "") or "") > 500:
            raise ValidationError("text_refinement max length is 500.")
        image_type = self._validate_image_type(edit_request.get("image_type") or CharacterImageType.PORTRAIT)
        self.characters.assert_identity_change_allowed(character, region, edit_request)
        self.safety.validate_user_text(edit_request.get("text_refinement", ""))
        variant_count = self._validate_variant_count(edit_request.get("variant_count", 4))

        # Identity-anchored generation: when a character has an EXPLICIT
        # identity source (canonical reference or uploaded photo), every edit
        # — including portrait — feeds that asset into the model as the
        # image-to-image input so the face stays the same across runs.
        #
        # Why portrait needs anchoring too: without it, every portrait re-edit
        # was text-only and the provider was free to draw a completely
        # different person, which then became the new active portrait. The
        # next full_body/scene edit was anchored on the (correct) canonical,
        # but the portrait tab kept showing the wrong face. This is what
        # produced the "asian woman in Portrait, anime girl in Full body,
        # brunette in Scene" data corruption we saw in the wild.
        #
        # Falling back to "latest portrait" is intentionally NOT used here for
        # portrait edits — that would re-anchor on whatever wrong face the
        # previous edit produced and never recover. Non-portrait edits keep
        # the legacy "any identity asset" behaviour so existing characters
        # without a canonical still get face consistency for full_body/scene.
        if image_type == CharacterImageType.PORTRAIT:
            identity_asset = self.characters.get_explicit_identity_asset(character)
        else:
            identity_asset = self.characters.get_identity_asset(character)
        if identity_asset is not None:
            return self._run_identity_anchored_edit(
                actor=user,
                character=character,
                image_type=image_type,
                region=region,
                identity_asset=identity_asset,
                variant_count=variant_count,
                edit_request=edit_request,
            )

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
        return self._run_job(
            user,
            character,
            GenerationJobType.EDIT_VARIANTS,
            region,
            variant_count,
            edit_request,
            compiled,
        )

    def _run_identity_anchored_edit(
        self,
        *,
        actor,
        character,
        image_type,
        region,
        identity_asset,
        variant_count,
        edit_request,
    ):
        """Edit a non-portrait view (full_body/scene/...) using identity asset as image input.

        Mirrors the pipeline used by :meth:`generate_reference` but stays under
        the EDIT_VARIANTS job type so the editor's caller (and its EDIT_DEPENDENCIES
        cascade) keeps working unchanged.
        """
        from pathlib import Path

        from django.conf import settings

        abs_path = Path(settings.MEDIA_ROOT) / identity_asset.storage_path
        try:
            reference_bytes = abs_path.read_bytes()
        except OSError:
            # Identity file disappeared on disk; fall back to text-only edit so
            # the user still gets a result instead of a hard failure.
            self.logger.warning(
                "Identity asset %s missing on disk; falling back to text-only edit "
                "for character=%s image_type=%s",
                identity_asset.asset_id, character.character_id, image_type,
            )
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
            payload = {**edit_request, "image_type": image_type,
                       "activate_image": edit_request.get("activate_image", True)}
            return self._run_job(
                actor,
                character,
                GenerationJobType.EDIT_VARIANTS,
                region,
                variant_count,
                payload,
                compiled,
            )
        mime_type = identity_asset.mime_type or "image/png"
        preserve_identity = bool(edit_request.get("preserve_identity", True))
        text_refinement = edit_request.get("text_refinement", "") or ""
        edit_controls = dict(edit_request.get("controls") or {})
        changed_fields = (
            edit_request.get("changed_fields")
            or edit_controls.get("changed_fields")
            or []
        )
        previous_values = (
            edit_request.get("previous_values")
            or edit_controls.get("previous_values")
            or {}
        )
        new_values = (
            edit_request.get("new_values")
            or edit_controls.get("new_values")
            or {}
        )

        compiled = self.compiler.compile_identity_anchored(
            character=character,
            appearance=character.active_appearance,
            outfit=character.active_outfit,
            image_type=image_type,
            params={
                "preserve_identity": preserve_identity,
                "text_refinement": text_refinement,
                "visual_style": edit_controls.get("visual_style"),
                "controls": edit_controls,
                "changed_fields": changed_fields,
                "previous_values": previous_values,
                "new_values": new_values,
            },
        )

        request_payload = {
            **edit_request,
            "image_type": image_type,
            "variant_count": variant_count,
            "preserve_identity": preserve_identity,
            "reference_asset_id": str(identity_asset.asset_id),
            "source_identity_asset_id": str(identity_asset.asset_id),
            "activate_image": edit_request.get("activate_image", True),
        }
        self.logger.info(
            "generate_edit_variants identity-anchored: character=%s image_type=%s identity_asset=%s",
            character.character_id, image_type, identity_asset.asset_id,
        )
        return self._run_job_with_reference(
            actor,
            character,
            request_payload,
            compiled,
            reference_bytes,
            mime_type,
            job_type=GenerationJobType.EDIT_VARIANTS,
        )

    def get_generation_job(self, user, job_id):
        from w_craft_back.character_studio.services.permissions import (
            get_viewable_project,
        )

        try:
            job = self.jobs.get(job_id=job_id)
        except CharacterGenerationJob.DoesNotExist as exc:
            raise NotFoundError("Generation job not found.") from exc
        get_viewable_project(user, job.project_id)
        return job

    def create_reference_variants(self, user, project_id, character_id, reference_asset, params):
        """Generate variants for a character seeded by a user-uploaded source image.

        Separate pipeline from :meth:`create_initial_variants`: uses a dedicated
        prompt compiler path and calls ``provider.generate_from_reference`` so the
        provider receives the actual reference bytes as multimodal input.
        """
        from pathlib import Path

        from django.conf import settings

        character = self.characters.get_generation_character(
            user, project_id, character_id,
        )
        if reference_asset.character_id != character.character_id:
            raise NotFoundError("Reference not found for character.")
        variant_count = self._validate_variant_count(params.get("variant_count", 4))
        preserve_identity = bool(params.get("preserve_identity", True))

        compiled = self.compiler.compile_reference_prompt(
            character=character,
            appearance=character.active_appearance,
            outfit=character.active_outfit,
            params={
                "preserve_identity": preserve_identity,
                "visual_style": params.get("visual_style"),
                "text_refinement": params.get("text_refinement") or params.get("refinement", ""),
            },
        )

        # Read reference bytes once before kicking off the job so a missing file
        # surfaces immediately, not buried in the provider call.
        storage_path = reference_asset.storage_path
        abs_path = Path(settings.MEDIA_ROOT) / storage_path
        try:
            reference_bytes = abs_path.read_bytes()
        except OSError as exc:
            raise ValidationError(
                "Reference image file is missing on storage."
            ) from exc
        mime_type = reference_asset.mime_type or "image/png"

        request_payload = {
            "variant_count": variant_count,
            "image_type": CharacterImageType.PORTRAIT,
            "image_model": params.get("image_model"),
            "preserve_identity": preserve_identity,
            "visual_style": params.get("visual_style"),
            "text_refinement": params.get("text_refinement") or params.get("refinement", ""),
            "reference_asset_id": str(reference_asset.asset_id),
            "activate_image": params.get("activate_image", True),
            "_idempotency_key": (params or {}).get("_idempotency_key"),
        }
        return self._run_job_with_reference(
            user,
            character,
            request_payload,
            compiled,
            reference_bytes,
            mime_type,
        )

    def _run_job_with_reference(
        self,
        actor,
        character,
        request_payload,
        compiled,
        reference_bytes=None,
        mime_type=None,
        job_type=GenerationJobType.REFERENCE_VARIANTS,
    ):
        return self._enqueue_and_execute(
            actor=actor,
            character=character,
            job_type=job_type,
            region=self.IMAGE_TYPE_TO_REGION.get(
                request_payload.get("image_type"),
                "full_character",
            ),
            variant_count=request_payload["variant_count"],
            request_payload=request_payload,
            compiled=compiled,
            provider_operation="reference",
            reference_bytes=reference_bytes,
            mime_type=mime_type,
        )

    def _run_job(
        self,
        actor,
        character,
        job_type,
        region,
        variant_count,
        request_payload,
        compiled,
    ):
        provider_operation = (
            "generate"
            if job_type == GenerationJobType.INITIAL_VARIANTS
            else "edit"
        )
        return self._enqueue_and_execute(
            actor=actor,
            character=character,
            job_type=job_type,
            region=region,
            variant_count=variant_count,
            request_payload=request_payload,
            compiled=compiled,
            provider_operation=provider_operation,
        )

    def _enqueue_and_execute(
        self,
        *,
        actor,
        character,
        job_type,
        region,
        variant_count,
        request_payload,
        compiled,
        provider_operation,
        reference_bytes=None,
        mime_type=None,
    ):
        self.safety.validate_generated_prompt(compiled["positive_prompt"])
        job = enqueue_job(
            actor=actor,
            character=character,
            job_type=job_type,
            region=region,
            variant_count=variant_count,
            request_payload=request_payload,
            compiled=compiled,
            provider_operation=provider_operation,
        )
        if not self.execute_immediately:
            return job
        return self.execute_queued_job(
            job.job_id,
            reference_bytes=reference_bytes,
            mime_type=mime_type,
        )

    def execute_queued_job(
        self,
        job_id,
        *,
        reference_bytes=None,
        mime_type=None,
    ):
        with log_context(job_id=job_id):
            return self._execute_queued_job(
                job_id,
                reference_bytes=reference_bytes,
                mime_type=mime_type,
            )

    def _execute_queued_job(
        self,
        job_id,
        *,
        reference_bytes=None,
        mime_type=None,
    ):
        """Run provider I/O outside transactions, fenced by a durable lease."""
        lease = claim_job(job_id)
        if lease is None:
            return CharacterGenerationJob.objects.get(job_id=job_id)

        job = (
            CharacterGenerationJob.objects.select_related(
                "actor",
                "actor__user",
                "character",
                "character__project",
            )
            .get(job_id=job_id)
        )
        compiled = {
            "positive_prompt": job.compiled_prompt,
            "negative_prompt": job.negative_prompt,
            "edit_instruction": job.edit_instruction,
            "metadata": dict(job.compiled_metadata or {}),
        }
        provider = None
        provider_started_at = time.monotonic()
        try:
            if job.provider_operation == "reference" and reference_bytes is None:
                reference_bytes, mime_type = self._load_reference_input(job)
            provider = get_image_provider(
                job.provider,
                provider_snapshot=job.provider_snapshot,
            )
            if not mark_provider_started(lease):
                return CharacterGenerationJob.objects.get(job_id=job_id)
            job.provider_deadline = time.monotonic() + lease.timeout_seconds
            job.provider_heartbeat = lambda: heartbeat_job(lease)
            if job.provider_operation == "reference":
                results = provider.generate_from_reference(
                    job,
                    compiled,
                    reference_bytes,
                    mime_type or "image/png",
                    job.variant_count,
                )
            elif job.provider_operation == "generate":
                results = provider.generate_character_variants(
                    job,
                    compiled,
                    job.variant_count,
                )
            elif job.provider_operation == "edit":
                results = provider.edit_character_region(
                    job,
                    compiled,
                    job.variant_count,
                )
            else:
                raise ValidationError(
                    f"Unsupported provider operation: {job.provider_operation}."
                )

            results = list(results or [])
            if not results:
                raise RuntimeError("Image provider returned no variants.")
            heartbeat_job(lease)
            self._complete_job(
                lease,
                results=results,
                provider=provider,
                duration_ms=round(
                    (time.monotonic() - provider_started_at) * 1000,
                    2,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            error_code, public_message = _generation_failure_details(exc)
            model = (
                getattr(provider, "model_version", "")
                or getattr(provider, "model_name", "")
                or job.provider
            )
            self.logger.error(
                "character_generation_failed",
                extra={
                    "job_id": job.job_id,
                    "model": model,
                    "duration_ms": round(
                        (time.monotonic() - provider_started_at) * 1000,
                        2,
                    ),
                    "status": "failed",
                    "error_code": error_code,
                },
            )
            fail_job(
                lease,
                error_code=error_code,
                error_message=public_message,
            )
        return CharacterGenerationJob.objects.get(job_id=job_id)

    def _load_reference_input(self, job):
        from pathlib import Path

        from django.conf import settings

        reference_asset_id = (job.request_payload or {}).get(
            "reference_asset_id"
        )
        if not reference_asset_id:
            raise ValidationError("Reference asset is missing from the job.")
        try:
            asset = CharacterAsset.objects.get(
                asset_id=reference_asset_id,
                character_id=job.character_id,
                project_id=job.project_id,
            )
        except CharacterAsset.DoesNotExist as exc:
            raise ValidationError(
                "Reference asset no longer belongs to this project."
            ) from exc
        try:
            reference_bytes = (
                Path(settings.MEDIA_ROOT) / asset.storage_path
            ).read_bytes()
        except OSError as exc:
            raise ValidationError(
                "Reference image file is missing on storage."
            ) from exc
        return reference_bytes, asset.mime_type or "image/png"

    @transaction.atomic
    def _complete_job(
        self,
        lease: JobLease,
        *,
        results,
        provider,
        duration_ms: float,
    ):
        """Persist assets and terminal state in one short fenced transaction."""
        job = (
            CharacterGenerationJob.objects.select_for_update(of=("self",))
            .select_related("actor", "character", "character__project")
            .get(job_id=lease.job_id)
        )
        if (
            job.status != GenerationJobStatus.PROCESSING
            or job.lease_token != lease.token
        ):
            return job

        character = job.character
        request_payload = dict(job.request_payload or {})
        compiled_metadata = dict(job.compiled_metadata or {})
        image_type = (
            request_payload.get("image_type")
            or CharacterImageType.PORTRAIT
        )
        reference_mode = job.provider_operation == "reference"
        if reference_mode:
            default_asset_type = CharacterAssetType.INITIAL_VARIANT
        elif job.job_type == GenerationJobType.INITIAL_VARIANTS:
            default_asset_type = CharacterAssetType.INITIAL_VARIANT
        else:
            default_asset_type = CharacterAssetType.EDIT_VARIANT
        asset_type = self.IMAGE_TYPE_TO_ASSET_TYPE.get(
            image_type,
            default_asset_type,
        )

        first_asset = None
        correction_prompt = (
            request_payload.get("correction_prompt") or ""
        ).strip()
        for item in results:
            metadata = {
                **(item.get("metadata") or {}),
                "image_type": image_type,
                "job_type": job.job_type,
                "edit_instruction": job.edit_instruction,
            }
            if reference_mode:
                metadata.update(
                    {
                        "reference_asset_id": request_payload.get(
                            "reference_asset_id"
                        ),
                        "source_identity_asset_id": request_payload.get(
                            "source_identity_asset_id"
                        )
                        or request_payload.get("reference_asset_id"),
                        "preserve_identity": request_payload.get(
                            "preserve_identity",
                            True,
                        ),
                    }
                )
            for key in ("edit_type", "selection", "instruction"):
                if request_payload.get(key):
                    metadata[key] = request_payload[key]
            if correction_prompt:
                metadata["correction_prompt"] = correction_prompt
                metadata["preserve_identity"] = bool(
                    request_payload.get("preserve_identity", False)
                )

            asset_kwargs = {
                "image_url": item["image_url"],
                "storage_path": item["storage_path"],
                "width": item["width"],
                "height": item["height"],
                "mime_type": item["mime_type"],
                "source": job.provider,
                "source_job_id": job.job_id,
                "generation_prompt": item["prompt"],
                "negative_prompt": item["negative_prompt"],
                "model_name": item["model_name"],
                "model_version": item["model_version"],
                "seed": item["seed"],
                "metadata": metadata,
                "safety_status": "passed",
            }
            if correction_prompt:
                asset_kwargs["correction_prompt"] = correction_prompt
            asset = self.assets.save_asset(
                job.actor,
                Action.RUN_GENERATION,
                character,
                asset_type,
                **asset_kwargs,
            )
            first_asset = first_asset or asset
            controls_snapshot = (
                request_payload
                if reference_mode
                else request_payload.get("controls", request_payload)
            )
            variant = self.variants.create(
                job=job,
                character=character,
                asset=asset,
                variant_index=item["variant_index"],
                region=job.region,
                controls_snapshot=controls_snapshot,
                appearance_snapshot=compiled_metadata,
                image_url=item["image_url"],
                prompt=item["prompt"],
                negative_prompt=item["negative_prompt"],
                seed=item["seed"],
                model_name=item["model_name"],
            )
            asset.source_variant_id = variant.variant_id
            asset.save(update_fields=["source_variant_id"])

        if first_asset and request_payload.get("activate_image", True):
            self._activate_image(
                character,
                first_asset,
                image_type,
                request_payload,
            )
        job.status = GenerationJobStatus.COMPLETED
        job.progress = 100
        job.model_name = getattr(provider, "model_name", "")
        job.model_version = getattr(provider, "model_version", "")
        job.completed_at = timezone.now()
        job.heartbeat_at = job.completed_at
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
        capture_provider_generation(
            domain="character",
            job_id=str(job.job_id),
            provider=provider,
        )
        self.logger.info(
            "character_generation_completed",
            extra={
                "job_id": job.job_id,
                "character_id": character.character_id,
                "model": job.model_version or job.model_name or job.provider,
                "duration_ms": duration_ms,
                "status": "completed",
            },
        )
        return job

    @staticmethod
    def recover_stale_jobs(*, limit=100):
        return recover_stale_jobs(limit=limit)

    @staticmethod
    def _scoped_idempotency_key(base_key, scope):
        base = str(base_key or "").strip()
        if not base:
            return ""
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]
        return f"{base[:90]}:{digest}:{scope}"[:128]

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
