from django.db import migrations, models


def set_existing_characters_active(apps, schema_editor):
    StudioCharacter = apps.get_model("w_craft_back", "StudioCharacter")
    StudioCharacter.objects.all().update(status="active")


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0014_character_role_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="studiocharacter",
            name="status",
            field=models.CharField(
                choices=[("draft", "Draft"), ("active", "Active")],
                default="draft",
                max_length=20,
                db_index=True,
            ),
        ),
        migrations.RunPython(set_existing_characters_active, migrations.RunPython.noop),
    ]
