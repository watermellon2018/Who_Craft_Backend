from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0007_harden_schema"),
    ]

    operations = [
        migrations.AddField(
            model_name="itemfolder",
            name="studio_character",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="w_craft_back.studiocharacter",
            ),
        ),
    ]
