from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0008_itemfolder_studio_character"),
    ]

    operations = [
        migrations.AddField(
            model_name="studiocharacter",
            name="character_type",
            field=models.CharField(
                choices=[
                    ("human", "Human"),
                    ("animal", "Animal"),
                    ("creature", "Creature"),
                    ("robot", "Robot"),
                    ("object", "Object"),
                    ("other", "Other"),
                ],
                default="human",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="studiocharacter",
            name="lifecycle_stage",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="characterappearance",
            name="body_structure",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="characterappearance",
            name="surface_material",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="characterappearance",
            name="special_features",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddConstraint(
            model_name="studiocharacter",
            constraint=models.CheckConstraint(
                check=models.Q(
                    ("character_type__in", ["human", "animal", "creature", "robot", "object", "other"])
                ),
                name="chk_studio_character_type",
            ),
        ),
    ]
