from django.db import transaction
from django.db.models import Max, Q

from w_craft_back.character_studio.models import (
    CharacterAppearance,
    CharacterAsset,
    CharacterExpression,
    CharacterGenerationJob,
    CharacterImage,
    CharacterOutfit,
    CharacterRelationship,
    CharacterRevision,
    CharacterVariant,
    CharacterVersion,
    StudioCharacter,
)
from w_craft_back.character_studio.repositories.base import BaseRepository


class CharacterRepository(BaseRepository):
    model = StudioCharacter

    def for_project_user(self, user, project_id):
        return self.model.objects.filter(project_id=project_id, user=user)

    def get_for_project_user(self, user, project_id, character_id):
        return self.for_project_user(user, project_id).get(character_id=character_id)

    def list_project(self, user, project_id, filters=None):
        filters = filters or {}
        queryset = self.for_project_user(user, project_id)
        if filters.get("role"):
            queryset = queryset.filter(role=filters["role"])
        if filters.get("search"):
            search = filters["search"]
            queryset = queryset.filter(Q(name__icontains=search) | Q(short_description__icontains=search))
        return queryset.order_by("-updated_at")


class AppearanceRepository(BaseRepository):
    model = CharacterAppearance


class OutfitRepository(BaseRepository):
    model = CharacterOutfit

    @transaction.atomic
    def set_default(self, character, outfit):
        self.model.objects.filter(character=character).update(is_default=False)
        outfit.is_default = True
        outfit.save(update_fields=["is_default", "updated_at"])
        return outfit


class VersionRepository(BaseRepository):
    model = CharacterVersion


class AssetRepository(BaseRepository):
    model = CharacterAsset

    @transaction.atomic
    def mark_as_primary(self, asset):
        self.model.objects.filter(character=asset.character).update(is_primary=False)
        asset.is_primary = True
        asset.save(update_fields=["is_primary"])
        return asset


class CharacterImageRepository(BaseRepository):
    model = CharacterImage

    @transaction.atomic
    def set_active(self, character, image_type, **payload):
        self.model.objects.filter(
            character=character,
            image_type=image_type,
            is_active=True,
        ).update(is_active=False)
        return self.model.objects.create(
            character=character,
            image_type=image_type,
            is_active=True,
            **payload,
        )


class GenerationJobRepository(BaseRepository):
    model = CharacterGenerationJob


class VariantRepository(BaseRepository):
    model = CharacterVariant

    def get_for_character(self, character, variant_id):
        return self.model.objects.select_related("job", "asset").get(
            character=character,
            variant_id=variant_id,
        )


class RevisionRepository(BaseRepository):
    model = CharacterRevision

    def next_revision_number(self, character):
        value = self.model.objects.filter(character=character).aggregate(Max("revision_number"))
        return (value["revision_number__max"] or 0) + 1


class ExpressionRepository(BaseRepository):
    model = CharacterExpression


class RelationshipRepository(BaseRepository):
    model = CharacterRelationship
