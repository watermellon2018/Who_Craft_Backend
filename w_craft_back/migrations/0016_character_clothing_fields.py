from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0015_add_character_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="studiocharacter",
            name="clothing_source",
            field=models.CharField(blank=True, default="text", max_length=20),
        ),
        migrations.AddField(
            model_name="studiocharacter",
            name="clothing_description",
            field=models.TextField(blank=True, default=""),
        ),
    ]
