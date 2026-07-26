from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0035_studiocharacter_model3d_autofit_version"),
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
                    ("three_quarter", "Three-quarter view"),
                    ("profile", "Profile view"),
                    ("back_view", "Back view"),
                    ("emotions_sheet", "Emotions sheet"),
                    ("poses_sheet", "Poses sheet"),
                    ("outfit_details", "Outfit details"),
                    ("expression", "Expression"),
                    ("outfit_reference", "Outfit reference"),
                    ("clothing_reference", "Clothing reference"),
                    ("thumbnail", "Thumbnail"),
                    ("model_3d", "3D model"),
                ],
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="charactergenerationjob",
            name="job_type",
            field=models.CharField(
                choices=[
                    ("initial_variants", "Initial variants"),
                    ("edit_variants", "Edit variants"),
                    ("outfit_variants", "Outfit variants"),
                    ("expression_variants", "Expression variants"),
                    ("character_sheet", "Character sheet"),
                    ("reference_extraction", "Reference extraction"),
                    ("reference_variants", "Reference-based variants"),
                    ("model3d_reconstruction", "3D reconstruction"),
                ],
                max_length=64,
            ),
        ),
    ]
