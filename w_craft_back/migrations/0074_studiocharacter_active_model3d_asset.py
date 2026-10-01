from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0073_video_source_identifier_length"),
    ]

    operations = [
        migrations.AddField(
            model_name="studiocharacter",
            name="active_model3d_asset",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="w_craft_back.characterasset",
            ),
        ),
    ]
