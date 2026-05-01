from copy import deepcopy

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from w_craft_back.character_studio.constants import VISUAL_STYLES
from w_craft_back.character_studio.models import CharacterImageType, CharacterType, RevisionChangeType
from w_craft_back.character_studio.repositories.repositories import (
    AppearanceRepository,
    CharacterImageRepository,
    CharacterRepository,
    OutfitRepository,
    VariantRepository,
)
from w_craft_back.character_studio.services.errors import IdentityLockedError, NotFoundError, ValidationError
from w_craft_back.character_studio.services.profile_parser import CharacterProfileParser
from w_craft_back.character_studio.services.revision_service import CharacterRevisionService
from w_craft_back.character_studio.services.safety import CharacterSafetyService
from w_craft_back.character_studio.services.serialization import character_dict


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
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValidationError("name is required.")
        self._validate_character_type(payload.get("character_type"))
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
        appearance_payload = parsed.get("appearance", {})
        appearance_payload.update(self._appearance_fields_from_payload(payload))
        appearance_payload.update(payload.get("appearance") or {})
        appearance = self.appearances.create(character=character, **appearance_payload)
        character.active_appearance = appearance
        character.save(update_fields=["active_appearance", "updated_at"])
        self.revisions.create_revision(
            character,
            RevisionChangeType.INITIAL_CREATE,
            changed_region="full_character",
            change_summary="Character created",
        )
        return character

    def get_character(self, user, project_id, character_id):
        try:
            return self.characters.get_for_project_user(user, project_id, character_id)
        except Exception as exc:
            raise NotFoundError("Character not found.") from exc

    def list_project_characters(self, user, project_id, filters=None):
        return [
            character_dict(character)
            for character in self.characters.list_project(user, project_id, filters).select_related(
                "canonical_reference_image"
            )
        ]

    @transaction.atomic
    def update_character(self, user, project_id, character_id, payload):
        character = self.get_character(user, project_id, character_id)
        self._validate_character_type(payload.get("character_type"))
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
            character,
            RevisionChangeType.MANUAL_UPDATE,
            changed_region="full_character",
            change_summary="Character metadata updated",
            before_snapshot=before,
        )
        return character

    @transaction.atomic
    def delete_character(self, user, project_id, character_id):
        character = self.get_character(user, project_id, character_id)
        character.delete()

    @transaction.atomic
    def duplicate_character(self, user, project_id, character_id):
        source = self.get_character(user, project_id, character_id)
        payload = character_dict(source, include_related=True)
        payload["name"] = f"{source.name} Copy"
        character = self.create_character(user, source.project, payload)
        if source.active_appearance:
            fields = character_dict(source, include_related=True).get("appearance", {})
            fields.pop("appearance_id", None)
        return character

    @transaction.atomic
    def lock_identity(self, user, project_id, character_id, payload):
        character = self.get_character(user, project_id, character_id)
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
            character,
            RevisionChangeType.IDENTITY_LOCK,
            changed_region="full_character",
            change_summary="Identity locked",
            before_snapshot=before,
        )
        return character

    @transaction.atomic
    def apply_variant(self, user, project_id, character_id, variant_id, payload):
        character = self.get_character(user, project_id, character_id)
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
        character.save()
        variant.applied = True
        variant.status = "applied"
        variant.applied_at = timezone.now()
        variant.save(update_fields=["applied", "status", "applied_at"])
        revision = self.revisions.create_revision(
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
            "height": "height",
            "body_type": "body_type",
            "body_structure": "body_structure",
            "surface_material": "surface_material",
            "special_features": "special_features",
            "posture": "posture",
            "distinctive_features": "distinctive_features",
        }
        result = {}
        for source, target in mapping.items():
            if source in payload:
                result[target] = payload[source] or ""
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
