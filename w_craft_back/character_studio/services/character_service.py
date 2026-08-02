import logging
from copy import deepcopy

from django.core.exceptions import ObjectDoesNotExist

logger = logging.getLogger(__name__)
from django.db import IntegrityError, transaction
from django.utils import timezone

from w_craft_back.character_studio.constants import VISUAL_STYLES
from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    CharacterImageType,
    CharacterRole,
    CharacterStatus,
    CharacterType,
    RevisionChangeType,
    StudioCharacter,
)
from w_craft_back.character_studio.repositories.repositories import (
    AppearanceRepository,
    CharacterImageRepository,
    CharacterRepository,
    OutfitRepository,
    VariantRepository,
)
from w_craft_back.character_studio.services.errors import (
    ConflictError,
    IdentityLockedError,
    NotFoundError,
    ValidationError,
)
from w_craft_back.character_studio.services.generation_lifecycle import (
    validate_idempotency_key,
)
from w_craft_back.character_studio.services.permissions import (
    get_editable_project,
    get_project_for_action,
    get_viewable_project,
)
from w_craft_back.character_studio.services.profile_parser import CharacterProfileParser
from w_craft_back.character_studio.services.revision_service import CharacterRevisionService
from w_craft_back.character_studio.services.safety import CharacterSafetyService
from w_craft_back.character_studio.services.serialization import character_dict
from w_craft_back.movie.project.policy import Action


class CharacterService:
    METADATA_FIELDS = {
        "name",
        "character_type",
        "role",
        "short_description",
        "age",
        "lifecycle_stage",
        "gender",
        "species",
        "visual_style",
        "personality",
        "speech_style",
        "backstory",
        "clothing_source",
        "clothing_description",
    }

    def __init__(self):
        self.characters = CharacterRepository()
        self.images = CharacterImageRepository()
        self.appearances = AppearanceRepository()
        self.outfits = OutfitRepository()
        self.variants = VariantRepository()
        self.revisions = CharacterRevisionService()
        self.parser = CharacterProfileParser()
        self.safety = CharacterSafetyService()

    @transaction.atomic
    def create_character(self, user, project, payload):
        project = get_editable_project(user, project.id)
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValidationError("name is required.")
        desc = payload.get("appearance_description") or ""
        logger.info(
            "create_character: user=%s project_id=%s name_len=%d desc_len=%d",
            getattr(user, "id", None), getattr(project, "id", None), len(name), len(desc),
        )
        self._validate_character_type(payload.get("character_type"))
        self._validate_role(payload.get("role"))
        self._validate_age(payload.get("age"))
        self._validate_style(payload.get("visual_style"))
        self.safety.validate_user_text(
            payload.get("short_description"),
            payload.get("appearance_description"),
            payload.get("lifecycle_stage"),
            payload.get("body_structure"),
            payload.get("surface_material"),
            payload.get("special_features"),
            payload.get("personality_description"),
            payload.get("backstory"),
        )
        parsed = self.parser.parse(payload.get("appearance_description") or payload.get("short_description", ""))
        personality = deepcopy(parsed.get("personality", {}))
        if payload.get("personality_description"):
            personality["description"] = payload["personality_description"]
        personality.update(payload.get("personality") or {})

        character = self.characters.create(
            project=project,
            user=user,
            name=name,
            character_type=payload.get("character_type") or CharacterType.HUMAN,
            role=payload.get("role", ""),
            short_description=payload.get("short_description", ""),
            age=self._normalized_age(payload.get("age")),
            lifecycle_stage=payload.get("lifecycle_stage", ""),
            gender=payload.get("gender", ""),
            species=payload.get("species") or payload.get("character_type") or "human",
            visual_style=payload.get("visual_style", ""),
            personality=personality,
            speech_style=payload.get("speech_style", ""),
            backstory=payload.get("backstory", ""),
        )
        logger.info("create_character: created character_id=%s", character.character_id)
        appearance_payload = parsed.get("appearance", {})
        appearance_payload.update(self._appearance_fields_from_payload(payload))
        appearance_payload.update(payload.get("appearance") or {})
        appearance = self.appearances.create(character=character, **appearance_payload)
        character.active_appearance = appearance
        character.save(update_fields=["active_appearance", "updated_at"])
        self.revisions.create_revision(
            user,
            Action.EDIT_CONTENT,
            character,
            RevisionChangeType.INITIAL_CREATE,
            changed_region="full_character",
            change_summary="Character created",
        )
        return character

    def create_character_from_reference(
        self,
        user,
        project,
        payload,
        uploaded_file,
        *,
        idempotency_key="",
        request_hash="",
    ):
        """Create or replay the character + uploaded-reference aggregate."""
        if not uploaded_file:
            raise ValidationError("reference_image is required.")
        project = get_editable_project(user, project.id)
        idempotency_key = validate_idempotency_key(idempotency_key)
        if idempotency_key and len(request_hash or "") != 64:
            raise ValidationError("A valid request hash is required.")

        replay = self._reference_creation_replay(
            user,
            project,
            idempotency_key,
            request_hash,
        )
        if replay is not None:
            return replay

        from w_craft_back.character_studio.services.asset_service import (
            CharacterAssetService,
        )

        try:
            with transaction.atomic():
                character = self.create_character(user, project, payload)
                if idempotency_key:
                    character.creation_idempotency_key = idempotency_key
                    character.creation_request_hash = request_hash
                    character.save(
                        update_fields=[
                            "creation_idempotency_key",
                            "creation_request_hash",
                            "updated_at",
                        ]
                    )
                reference_asset = (
                    CharacterAssetService().save_uploaded_source_reference(
                        character,
                        user,
                        uploaded_file,
                    )
                )
                return character, reference_asset
        except IntegrityError:
            if not idempotency_key:
                raise
            replay = self._reference_creation_replay(
                user,
                project,
                idempotency_key,
                request_hash,
            )
            if replay is None:
                raise
            return replay

    @staticmethod
    def _reference_creation_replay(
        user,
        project,
        idempotency_key,
        request_hash,
    ):
        if not idempotency_key:
            return None
        character = (
            StudioCharacter.objects.filter(
                project=project,
                user=user,
                creation_idempotency_key=idempotency_key,
            )
            .order_by("created_at")
            .first()
        )
        if character is None:
            return None
        if character.creation_request_hash != request_hash:
            raise ConflictError(
                "Idempotency-Key was already used for a different request."
            )
        reference_asset = (
            character.assets.filter(
                asset_type=CharacterAssetType.UPLOADED_REFERENCE,
            )
            .order_by("created_at")
            .first()
        )
        if reference_asset is None:
            raise ConflictError("Reference character creation is incomplete.")
        return character, reference_asset

    def _get_character_for_action(
        self, user, project_id, character_id, action: Action,
    ):

        get_project_for_action(user, project_id, action)
        try:
            return self.characters.get_for_project_user(user, project_id, character_id)
        except Exception as exc:
            raise NotFoundError("Character not found.") from exc

    def get_viewable_character(self, user, project_id, character_id):
        return self._get_character_for_action(
            user, project_id, character_id, Action.VIEW,
        )

    def get_editable_character(self, user, project_id, character_id):
        return self._get_character_for_action(
            user, project_id, character_id, Action.EDIT_CONTENT,
        )

    def get_generation_character(self, user, project_id, character_id):
        return self._get_character_for_action(
            user, project_id, character_id, Action.RUN_GENERATION,
        )


    def get_identity_asset(self, character):
        """Find the best identity-source asset for this character, or None.

        Priority chain (first match wins):
            1. character.canonical_reference_image — the user's explicit choice
               (set via apply_variant(apply_as='current_reference') or
               lock_identity()). This is the strongest signal of "the official
               look of this character".
            2. Latest READY ``UPLOADED_REFERENCE`` asset — for characters created
               via the from-reference flow, where the user gave us a real photo.
            3. Latest READY ``PORTRAIT`` asset — best-effort fallback when the
               user generated portrait variants but didn't explicitly mark one
               canonical.

        Returns ``None`` if no identity source exists yet — callers should
        translate that into an IdentityAssetRequiredError.
        """
        return self._identity_asset(character, allow_portrait_fallback=True)

    def get_explicit_identity_asset(self, character):
        """Like :meth:`get_identity_asset` but skips the latest-portrait fallback.

        For portrait edits we MUST NOT anchor on the latest portrait (that's
        the very thing we're editing — anchoring on it would lock in whatever
        random face the previous edit produced). Only an explicit identity
        source — canonical reference or uploaded reference — should drive a
        portrait-edit's image-to-image input.
        """
        return self._identity_asset(character, allow_portrait_fallback=False)

    def _identity_asset(self, character, *, allow_portrait_fallback):
        canonical = character.canonical_reference_image
        if canonical and canonical.status == CharacterAssetStatus.READY:
            return canonical

        uploaded = (
            CharacterAsset.objects
            .filter(
                character=character,
                asset_type=CharacterAssetType.UPLOADED_REFERENCE,
                status=CharacterAssetStatus.READY,
            )
            .order_by("-created_at")
            .first()
        )
        if uploaded:
            return uploaded

        if not allow_portrait_fallback:
            return None

        portrait = (
            CharacterAsset.objects
            .filter(
                character=character,
                asset_type=CharacterAssetType.PORTRAIT,
                status=CharacterAssetStatus.READY,
            )
            .order_by("-version", "-created_at")
            .first()
        )
        return portrait

    def list_project_characters(self, user, project_id, filters=None):
        # Gate project view-access before listing (repo scopes by project only).
        get_viewable_project(user, project_id)
        return [
            character_dict(character)
            for character in self.characters.list_project(user, project_id, filters).select_related(
                "canonical_reference_image"
            )
        ]

    @transaction.atomic
    def update_character(self, user, project_id, character_id, payload):
        character = self.get_editable_character(user, project_id, character_id)
        self._validate_character_type(payload.get("character_type"))
        self._validate_role(payload.get("role"))
        self._validate_age(payload.get("age"))
        self._validate_style(payload.get("visual_style"))
        self.safety.validate_user_text(
            payload.get("short_description"),
            payload.get("lifecycle_stage"),
            payload.get("body_structure"),
            payload.get("surface_material"),
            payload.get("special_features"),
            payload.get("speech_style"),
            payload.get("backstory"),
        )
        updates = {key: payload[key] for key in self.METADATA_FIELDS if key in payload}
        before = character_dict(character, include_related=True)
        for key, value in updates.items():
            if key == "age":
                value = self._normalized_age(value)
            setattr(character, key, value)
        character.save()
        appearance_updates = self._appearance_fields_from_payload(payload)
        if payload.get("appearance"):
            appearance_updates.update(payload["appearance"])
        if appearance_updates:
            appearance = character.active_appearance or self.appearances.create(character=character)
            for key, value in appearance_updates.items():
                setattr(appearance, key, value)
            appearance.save()
            if not character.active_appearance_id:
                character.active_appearance = appearance
                character.save(update_fields=["active_appearance", "updated_at"])
        self.revisions.create_revision(
            user,
            Action.EDIT_CONTENT,
            character,
            RevisionChangeType.MANUAL_UPDATE,
            changed_region="full_character",
            change_summary="Character metadata updated",
            before_snapshot=before,
        )
        return character

    @transaction.atomic
    def delete_character(self, user, project_id, character_id):
        character = self.get_editable_character(user, project_id, character_id)
        character.delete()

    @transaction.atomic
    def lock_identity(self, user, project_id, character_id, payload):
        character = self.get_editable_character(user, project_id, character_id)
        if not payload.get("confirm"):
            raise ValidationError("confirm=true is required to lock identity.")
        before = character_dict(character, include_related=True)
        reference_id = payload.get("reference_image_id")
        appearance_id = payload.get("appearance_id")
        try:
            if reference_id:
                character.canonical_reference_image = character.assets.get(asset_id=reference_id)
            if appearance_id:
                character.active_appearance = character.appearances.get(appearance_id=appearance_id)
        except ObjectDoesNotExist as exc:
            raise NotFoundError("Reference image or appearance not found.") from exc
        character.identity_locked = True
        character.locked_at = timezone.now()
        character.locked_by = user
        character.save()
        self.revisions.create_revision(
            user,
            Action.EDIT_CONTENT,
            character,
            RevisionChangeType.IDENTITY_LOCK,
            changed_region="full_character",
            change_summary="Identity locked",
            before_snapshot=before,
        )
        return character

    @transaction.atomic
    def apply_variant(self, user, project_id, character_id, variant_id, payload):
        character = self.get_editable_character(user, project_id, character_id)
        try:
            variant = self.variants.get_for_character(character, variant_id)
        except Exception as exc:
            raise NotFoundError("Variant not found for character.") from exc
        before = character_dict(character, include_related=True)
        asset = variant.asset
        if asset:
            image_type = self._image_type_from_payload(payload, variant)
            apply_as = payload.get("apply_as")
            if apply_as == "current_reference":
                asset.__class__.objects.filter(character=character).update(is_primary=False, is_canonical=False)
                asset.is_primary = True
                asset.is_canonical = True
                character.canonical_reference_image = asset
            elif apply_as == "canonical_reference":
                asset.__class__.objects.filter(character=character).update(is_canonical=False)
                asset.is_primary = False
                asset.is_canonical = True
                character.canonical_reference_image = asset
            else:
                asset.is_primary = False
            asset.save(update_fields=["is_primary", "is_canonical"])
            self.images.set_active(
                character,
                image_type,
                asset=asset,
                image_url=asset.image_url,
                storage_path=asset.storage_path,
                prompt=asset.generation_prompt,
                seed=asset.seed,
                generation_params={
                    "applied_variant_id": str(variant.variant_id),
                    "source_job_id": str(variant.job_id),
                    "image_type": image_type,
                },
            )
        # Applying a variant is the user's explicit "this is the character I
        # want" signal — that's when a draft graduates to a visible character.
        if character.status == CharacterStatus.DRAFT:
            character.status = CharacterStatus.ACTIVE
        character.save()
        variant.applied = True
        variant.status = "applied"
        variant.applied_at = timezone.now()
        variant.save(update_fields=["applied", "status", "applied_at"])
        revision = self.revisions.create_revision(
            user,
            Action.EDIT_CONTENT,
            character,
            RevisionChangeType.APPLY_VARIANT,
            source_variant=variant,
            source_job=variant.job,
            reference_image=asset,
            changed_region=variant.region,
            change_summary=payload.get("notes", "Applied character variant"),
            text_refinement=variant.job.request_payload.get("text_refinement", ""),
            before_snapshot=before,
        )
        return revision

    def _validate_age(self, age):
        if age in (None, ""):
            return
        try:
            age_value = int(age)
        except (TypeError, ValueError) as exc:
            raise ValidationError("age must be a number.") from exc
        if age_value < 0 or age_value > 130:
            raise ValidationError("age must be between 0 and 130.")

    def _normalized_age(self, age):
        if age in (None, ""):
            return None
        return int(age)

    def _validate_style(self, style):
        if not style:
            return
        if style not in VISUAL_STYLES and len(style) > 80:
            raise ValidationError("visual_style must be known or a concise custom value.")

    def _validate_character_type(self, character_type):
        if not character_type:
            return
        if character_type not in CharacterType.values:
            raise ValidationError("character_type is invalid.")

    def _validate_role(self, role):
        if not role:
            return
        if role not in CharacterRole.values:
            raise ValidationError(f"role is invalid. Allowed values: {', '.join(CharacterRole.values)}.")

    def _appearance_fields_from_payload(self, payload):
        mapping = {
            "appearance_description": "appearance_prompt",
            "face_shape": "face_shape",
            "skin_tone": "skin_tone",
            "eye_shape": "eye_shape",
            "eye_color": "eye_color",
            "eyebrow_shape": "eyebrow_shape",
            "nose_shape": "nose_shape",
            "lips_shape": "lips_shape",
            "jawline": "jawline",
            "hair_length": "hair_length",
            "hair_style": "hair_style",
            "hair_color": "hair_color",
            "hair_details": "hair_details",
            "height": "height",
            "height_cm": "height_cm",
            "body_type": "body_type",
            "body_structure": "body_structure",
            "surface_material": "surface_material",
            "special_features": "special_features",
            "posture": "posture",
            "distinctive_features": "distinctive_features",
        }
        result = {}
        # Allow explicit clearing of free-text and list fields; for shape/color fields
        # an empty value means "not present in this PATCH" — must NOT overwrite saved data.
        empty_ok = {"appearance_description", "distinctive_features", "hair_details"}
        for source, target in mapping.items():
            if source not in payload:
                continue
            value = payload[source]
            if value in (None, "") and source not in empty_ok:
                continue
            if target == "height_cm":
                # Coerce + clamp to a sane range; PositiveSmallIntegerField accepts 0–32767.
                try:
                    coerced = int(value)
                except (TypeError, ValueError):
                    continue
                if coerced < 50 or coerced > 280:
                    continue
                result[target] = coerced
                continue
            result[target] = value if value is not None else ""
        if "appearance_description" in payload:
            result["source_type"] = "description"
            result["source_description"] = payload.get("appearance_description") or ""
        return result

    def assert_identity_change_allowed(self, character, region, payload):
        if not character.identity_locked:
            return
        if region in ("face", "full_character") and not (
            payload.get("confirm_identity_change") or payload.get("create_version")
        ):
            raise IdentityLockedError()

    def _image_type_from_payload(self, payload, variant):
        value = payload.get("image_type")
        if not value and variant.asset and variant.asset.metadata:
            value = variant.asset.metadata.get("image_type")
        if not value and variant.job and variant.job.request_payload:
            value = variant.job.request_payload.get("image_type")
        normalized = {
            "fullBody": CharacterImageType.FULL_BODY,
            "sheet": CharacterImageType.REFERENCE_SHEET,
            "character_sheet": CharacterImageType.REFERENCE_SHEET,
        }.get(value, value or CharacterImageType.PORTRAIT)
        if normalized not in CharacterImageType.values:
            raise ValidationError("image_type is invalid.")
        return normalized
