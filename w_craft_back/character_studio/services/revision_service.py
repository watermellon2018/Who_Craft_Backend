from django.db import transaction

from w_craft_back.character_studio.models import (
    CharacterRegion,
    RevisionChangeType,
    StudioCharacter,
)
from w_craft_back.character_studio.repositories.repositories import RevisionRepository
from w_craft_back.character_studio.services.permissions import get_project_for_action
from w_craft_back.character_studio.services.serialization import character_dict, revision_dict
from w_craft_back.movie.project.policy import Action


class CharacterRevisionService:
    def __init__(self, repository=None):
        self.revisions = repository or RevisionRepository()

    @transaction.atomic
    def create_revision(
        self,
        actor,
        action,
        character,
        change_type,
        snapshot=None,
        source_variant=None,
        source_job=None,
        reference_image=None,
        appearance=None,
        outfit=None,
        version=None,
        changed_region=CharacterRegion.FULL_CHARACTER,
        change_summary="",
        text_refinement="",
        before_snapshot=None,
    ):
        if action is not Action.EDIT_CONTENT:
            raise ValueError(
                f"Revision mutation requires edit_content, received {action.value}"
            )
        get_project_for_action(actor, character.project_id, action)
        character = StudioCharacter.objects.select_for_update().get(
            pk=character.pk,
        )
        number = self.revisions.next_revision_number(character)
        after_snapshot = snapshot or character_dict(character, include_related=True)
        revision = self.revisions.create(
            character=character,
            project=character.project,
            user=actor,
            revision_number=number,
            source_variant=source_variant,
            source_job=source_job,
            reference_image=reference_image or character.canonical_reference_image,
            appearance=appearance or character.active_appearance,
            outfit=outfit or character.active_outfit,
            version=version or character.active_version,
            change_type=change_type,
            changed_region=changed_region or CharacterRegion.FULL_CHARACTER,
            change_summary=change_summary,
            text_refinement=text_refinement or "",
            before_snapshot=before_snapshot or {},
            after_snapshot=after_snapshot,
        )
        character.current_revision = revision
        character.save(update_fields=["current_revision", "updated_at"])
        return revision

    def list_revisions(self, character):
        return [revision_dict(revision) for revision in character.revisions.order_by("-revision_number")]

    @transaction.atomic
    def restore_revision(self, actor, action, character, revision):
        if action is not Action.EDIT_CONTENT:
            raise ValueError(
                f"Revision mutation requires edit_content, received {action.value}"
            )
        get_project_for_action(actor, character.project_id, action)
        character = StudioCharacter.objects.select_for_update().get(
            pk=character.pk,
        )
        before = character_dict(character, include_related=True)
        if revision.reference_image:
            character.canonical_reference_image = revision.reference_image
        if revision.appearance:
            character.active_appearance = revision.appearance
        if revision.outfit:
            character.active_outfit = revision.outfit
        if revision.version:
            character.active_version = revision.version
        character.save()
        return self.create_revision(
            actor,
            action,
            character,
            RevisionChangeType.RESTORE_REVISION,
            source_variant=revision.source_variant,
            source_job=revision.source_job,
            reference_image=revision.reference_image,
            appearance=revision.appearance,
            outfit=revision.outfit,
            version=revision.version,
            changed_region=revision.changed_region,
            change_summary=f"Restored revision {revision.revision_number}",
            before_snapshot=before,
        )

    def get_current_revision(self, character):
        return revision_dict(character.current_revision)

