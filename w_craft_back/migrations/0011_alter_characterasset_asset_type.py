from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0010_character_images"),
    ]

    operations = [
        migrations.AlterField(
            model_name="characterasset",
            name="asset_type",
            field=models.CharField(
                choices=[
                    ("uploaded_reference", "Uploaded reference"),
                    ("initial_variant", "Initial variant"),
                    ("edit_variant", "Edit variant"),
                    ("canonical_reference", "Canonical reference"),
                    ("portrait", "Portrait"),
                    ("full_body", "Full body"),
                    ("scene", "Scene"),
                    ("reference_sheet", "Reference sheet"),
                    ("face_closeup", "Face closeup"),
                    ("front_view", "Front view"),
                    ("side_view", "Side view"),
                    ("expression", "Expression"),
                    ("outfit_reference", "Outfit reference"),
                    ("thumbnail", "Thumbnail"),
                ],
                max_length=64,
            ),
        ),
    ]
