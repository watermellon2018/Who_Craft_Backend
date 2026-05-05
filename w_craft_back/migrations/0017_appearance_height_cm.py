from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0016_character_clothing_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="characterappearance",
            name="height_cm",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
