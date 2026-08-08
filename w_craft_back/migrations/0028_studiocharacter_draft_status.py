"""Reintroduce 'draft' status to StudioCharacter + clean up tree leftovers.

Why this migration exists
-------------------------
Migration 0024 deleted the ``draft`` status because at the time it wasn't
preventing duplicate-row symptoms in the UI. The duplicates persisted because
new characters were persisted with the final ``active`` status the moment the
user clicked "Generate" — every cancelled/restarted creation attempt left a
real row behind, and the legacy ``MenuFolder`` tree showed them all.

Reintroducing ``draft`` with the right lifecycle hooks (``create_character``
sets it; ``apply_variant`` promotes to ``active``) plus tree/list filtering
that excludes drafts is what actually fixes the duplicate-name problem.

Data steps
----------
1. Backfill: any character that already has an active CharacterImage OR an
   explicitly set canonical_reference_image stays ``active`` — those are
   confirmed characters. Any character with neither, AND no applied variants,
   is downgraded to ``draft`` (and will disappear from the gallery/tree
   automatically because of the filter introduced in this PR).
2. Dangling tree nodes: ``ItemFolder`` rows that are not folders AND have no
   ``studio_character`` link AND no legacy ``hero`` link are removed — they
   are tree-side artifacts from creation flows the user bailed out of.

Schema step
-----------
Choices change from {active, references_locked} to
{draft, active, references_locked}. Default changes from ``active`` to
``draft`` so future creations land in draft by default.
"""

from django.db import migrations, models


def backfill_status_and_cleanup_tree(apps, schema_editor):
    StudioCharacter = apps.get_model("w_craft_back", "StudioCharacter")
    CharacterImage = apps.get_model("w_craft_back", "CharacterImage")
    CharacterVariant = apps.get_model("w_craft_back", "CharacterVariant")
    ItemFolder = apps.get_model("w_craft_back", "ItemFolder")

    # Confirmed characters: have at least one active rendered image OR an
    # explicitly applied variant OR a canonical reference set.
    confirmed_ids = set(
        CharacterImage.objects
        .filter(is_active=True)
        .values_list("character_id", flat=True)
    )
    confirmed_ids.update(
        CharacterVariant.objects
        .filter(applied=True)
        .values_list("character_id", flat=True)
    )
    confirmed_ids.update(
        StudioCharacter.objects
        .filter(canonical_reference_image__isnull=False)
        .values_list("character_id", flat=True)
    )

    # Everything currently 'active' that is NOT confirmed gets demoted to draft.
    StudioCharacter.objects.filter(status="active").exclude(
        character_id__in=confirmed_ids,
    ).update(status="draft")

    # Tree-side cleanup: leaves with no link at all (no studio_character, no hero)
    # are creation-flow artifacts the user never finished. They produce ghost
    # entries in the tree with no editor target. Safe to delete — there's no
    # data attached.
    ItemFolder.objects.filter(
        is_folder=False,
        studio_character__isnull=True,
        hero__isnull=True,
    ).delete()


def restore_active_default(apps, schema_editor):
    # Reverse: promote every draft back to active so the field shape from
    # 0024 is recoverable. We can't distinguish "demoted by this migration"
    # from "created as draft after this migration" — promoting everything is
    # the safest choice for a reverse.
    StudioCharacter = apps.get_model("w_craft_back", "StudioCharacter")
    StudioCharacter.objects.filter(status="draft").update(status="active")


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0027_userprofile_image_generation_model"),
    ]

    operations = [
        migrations.AlterField(
            model_name="studiocharacter",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("active", "Active"),
                    ("references_locked", "References locked"),
                ],
                db_index=True,
                default="draft",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_status_and_cleanup_tree, restore_active_default),
    ]
