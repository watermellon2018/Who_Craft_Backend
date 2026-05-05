from django.db import migrations, models


ROLE_MAP = {
    # main
    "главный герой": "main",
    "главная героиня": "main",
    "protagonist": "main",
    "main_character": "main",
    "main": "main",
    # secondary
    "второстепенный персонаж": "secondary",
    "второстепенный": "secondary",
    "supporting": "secondary",
    "secondary": "secondary",
    # antagonist
    "антагонист": "antagonist",
    "antagonist": "antagonist",
    "villain": "antagonist",
    "enemy": "antagonist",
    # episodic
    "эпизодический": "episodic",
    "episodic": "episodic",
    # cameo
    "камео": "cameo",
    "cameo": "cameo",
}


def normalize_roles(apps, schema_editor):
    StudioCharacter = apps.get_model("w_craft_back", "StudioCharacter")
    for character in StudioCharacter.objects.exclude(role=""):
        normalized = ROLE_MAP.get(character.role.lower().strip())
        if normalized is None:
            normalized = "secondary"
        if normalized != character.role:
            character.role = normalized
            character.save(update_fields=["role"])


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0013_remove_studiocharacter_characters_status_49cf2d_idx_and_more"),
    ]

    operations = [
        migrations.RunPython(normalize_roles, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="studiocharacter",
            name="role",
            field=models.CharField(
                blank=True,
                default="",
                max_length=100,
                choices=[
                    ("main", "Главный герой"),
                    ("secondary", "Второстепенный персонаж"),
                    ("antagonist", "Антагонист"),
                    ("episodic", "Эпизодический"),
                    ("cameo", "Камео"),
                ],
            ),
        ),
    ]
