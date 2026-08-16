import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Count, F


LEGACY_MODELS = (
    "BiographyRelationships",
    "GoalsMotivation",
    "PersonalityTraits",
    "ProfessionHobbies",
    "TalentsAbilities",
    "GraphEdge",
    "RelationshipType",
    "Character",
)


def require_empty_legacy_character_tables(apps, schema_editor) -> None:
    """Abort before destructive schema changes if any legacy data remains."""

    database = schema_editor.connection.alias
    populated = []
    for model_name in LEGACY_MODELS:
        model = apps.get_model("w_craft_back", model_name)
        if model.objects.using(database).exists():
            populated.append(model_name)

    ItemFolder = apps.get_model("w_craft_back", "ItemFolder")
    MenuFolder = apps.get_model("w_craft_back", "MenuFolder")
    if ItemFolder.objects.using(database).filter(hero_id__isnull=False).exists():
        populated.append("ItemFolder.hero")
    if MenuFolder.objects.using(database).filter(cur_project_id__isnull=True).exists():
        populated.append("MenuFolder.cur_project")
    if MenuFolder.objects.using(database).exclude(
        parent_id__isnull=True
    ).exclude(
        parent__cur_project_id=F("cur_project_id")
    ).exists():
        populated.append("MenuFolder.parent.project")
    if ItemFolder.objects.using(database).exclude(
        studio_character_id__isnull=True
    ).exclude(
        cur_project_id__isnull=True
    ).exclude(
        studio_character__project_id=F("cur_project_id")
    ).exists():
        populated.append("ItemFolder.studio_character.project")
    if ItemFolder.objects.using(database).exclude(
        studio_character_id__isnull=True
    ).values(
        "studio_character_id"
    ).annotate(
        placements=Count("pk")
    ).filter(placements__gt=1).exists():
        populated.append("ItemFolder.studio_character.duplicate")

    if populated:
        names = ", ".join(populated)
        raise RuntimeError(
            "Legacy character removal requires empty tables and links; "
            f"found data in: {names}. Migrate or remove the data before retrying."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0051_image_model_flexibility"),
    ]

    operations = [
        migrations.RunPython(
            require_empty_legacy_character_tables,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="menufolder",
            name="cur_project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to="w_craft_back.project",
            ),
        ),
        migrations.AlterField(
            model_name="itemfolder",
            name="studio_character",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tree_placement",
                to="w_craft_back.studiocharacter",
            ),
        ),
        migrations.RemoveField(
            model_name="itemfolder",
            name="hero",
        ),
        migrations.RemoveField(
            model_name="menufolder",
            name="user",
        ),
        migrations.DeleteModel(name="BiographyRelationships"),
        migrations.DeleteModel(name="GoalsMotivation"),
        migrations.DeleteModel(name="PersonalityTraits"),
        migrations.DeleteModel(name="ProfessionHobbies"),
        migrations.DeleteModel(name="TalentsAbilities"),
        migrations.DeleteModel(name="GraphEdge"),
        migrations.DeleteModel(name="RelationshipType"),
        migrations.DeleteModel(name="Character"),
    ]
