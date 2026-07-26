"""Re-sync active portrait CharacterImage to the canonical reference asset.

Why
---
Before this PR, every portrait edit re-generated through a text-only
pipeline that didn't see the character's identity-source image. When the
provider drew a completely different face, that face was persisted as the
new ``CharacterImage(image_type='portrait', is_active=True)`` row — so the
editor's Portrait tab started showing a stranger even though
``canonical_reference_image`` still pointed at the correct face.

The generation-side fix (now anchoring portrait edits on the canonical
asset) prevents this from happening again, but doesn't repair characters
that already drifted. This data migration repairs them by making the
active portrait image point back at the canonical asset whenever the two
diverge.

Scope
-----
Only characters that have ``canonical_reference_image`` set are touched.
Without an explicit identity anchor the user never declared what the
"correct" face is, so we don't second-guess the data.

Reverse
-------
Non-reversible: we can't reconstruct the previous active-image pointer
without keeping a snapshot. The reverse is a no-op — re-applying a
specific portrait variant is a one-click action in the editor.
"""

from django.db import migrations


def resync_portrait_to_canonical(apps, schema_editor):
    StudioCharacter = apps.get_model("w_craft_back", "StudioCharacter")
    CharacterImage = apps.get_model("w_craft_back", "CharacterImage")

    characters = StudioCharacter.objects.exclude(canonical_reference_image__isnull=True)
    for character in characters.iterator():
        canonical = character.canonical_reference_image
        if canonical is None:
            continue

        active_portrait = (
            CharacterImage.objects
            .filter(character=character, image_type="portrait", is_active=True)
            .first()
        )
        if active_portrait and str(active_portrait.asset_id) == str(canonical.asset_id):
            # Already in sync.
            continue

        # Deactivate any active portrait that points elsewhere.
        CharacterImage.objects.filter(
            character=character, image_type="portrait", is_active=True,
        ).update(is_active=False)

        # Either flip an existing image-row that already references the
        # canonical asset, or create a fresh active row for it.
        existing = (
            CharacterImage.objects
            .filter(character=character, image_type="portrait", asset_id=canonical.asset_id)
            .first()
        )
        if existing:
            existing.is_active = True
            existing.save(update_fields=["is_active", "updated_at"])
        else:
            CharacterImage.objects.create(
                character=character,
                image_type="portrait",
                asset_id=canonical.asset_id,
                image_url=canonical.image_url,
                storage_path=canonical.storage_path,
                prompt=canonical.generation_prompt or "",
                seed=canonical.seed,
                generation_params={
                    "resynced_from_canonical": True,
                    "image_type": "portrait",
                },
                is_active=True,
            )


def noop(apps, schema_editor):
    # Reverse: we don't keep the previous pointer, so there's nothing to
    # restore. Leaving as a no-op rather than refusing the reverse keeps
    # local rollbacks unblocked.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0028_studiocharacter_draft_status"),
    ]

    operations = [
        migrations.RunPython(resync_portrait_to_canonical, noop),
    ]
