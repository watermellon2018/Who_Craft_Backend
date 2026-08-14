from django.db import transaction
from django.db.models import Max, Q

from w_craft_back.character_studio.models import (
    CharacterAppearance,
    CharacterAsset,
    CharacterGenerationJob,
    CharacterImage,
    CharacterOutfit,
    CharacterRevision,
    CharacterVariant,
    StudioCharacter,
    VISIBLE_CHARACTER_STATUSES,
)
from w_craft_back.character_studio.repositories.base import BaseRepository


class CharacterRepository(BaseRepository):
    model = StudioCharacter

    def for_project_user(self, user, project_id):
        # Characters belong to the PROJECT, not the individual member who
        # created them (team collaboration): scope by project only. Callers
        # MUST gate project access through the project policy first (the
        # character service does this via get_owned_project / require_project_edit).
        # The ``user`` argument is kept for signature back-compat.
        return self.model.objects.filter(project_id=project_id)

    def get_for_project_user(self, user, project_id, character_id):
        return self.for_project_user(user, project_id).get(character_id=character_id)

    def list_project(self, user, project_id, filters=None):
        filters = filters or {}
        queryset = self.for_project_user(user, project_id)
        # status filter semantics:
        #   - omitted / "visible" → only user-confirmed characters (gallery + tree)
        #   - "all" → no filter (admin / migration tools)
        #   - any specific status → exact match (rare, mostly tests)
        # NOTE: "active" used to mean "default" but now we use "visible" to
        # distinguish the meaning from the literal status value.
        status_filter = filters.get("status", "visible")
        if status_filter == "visible":
            queryset = queryset.filter(status__in=VISIBLE_CHARACTER_STATUSES)
        elif status_filter != "all":
            queryset = queryset.filter(status=status_filter)
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
