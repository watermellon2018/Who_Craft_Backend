"""Remove the 'draft' status from StudioCharacter.

Data migration:
- Empty drafts (status='draft' AND canonical_reference_image_id IS NULL) are
  deleted as garbage. They are unfinished UI artifacts with no value.
- Remaining drafts are converted to 'active'.

Schema migration:
- Field choices change from {draft, active, references_locked} to
  {active, references_locked}.
- Default changes from 'draft' to 'active'.

The 'references_locked' value is preserved untouched.
"""

from django.db import migrations, models


def cleanup_drafts(apps, schema_editor):
    StudioCharacter = apps.get_model("w_craft_back", "StudioCharacter")
    # Delete empty drafts (orphaned UI artifacts).
    StudioCharacter.objects.filter(
        status="draft", canonical_reference_image__isnull=True
    ).delete()
    # Promote remaining drafts to active.
    StudioCharacter.objects.filter(status="draft").update(status="active")


def restore_drafts(apps, schema_editor):
    # Reverse migration is a no-op: we cannot resurrect deleted rows or
    # know which 'active' rows used to be drafts. Leaving as-is is safe.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0023_location_musictrack_projectactivity_projectasset_and_more"),
    ]

    operations = [
        migrations.RunPython(cleanup_drafts, restore_drafts),
        migrations.AlterField(
            model_name="studiocharacter",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "Active"),
                    ("references_locked", "References locked"),
                ],
                db_index=True,
                default="active",
                max_length=20,
            ),
        ),
    ]
