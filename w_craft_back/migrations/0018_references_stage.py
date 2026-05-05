from django.db import migrations, models
from django.db.models import Q


ASSET_TYPE_CHOICES = [
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
]


IMAGE_TYPE_CHOICES = [
    ("portrait", "Portrait"),
    ("full_body", "Full body"),
    ("scene", "Scene"),
    ("reference_sheet", "Reference sheet"),
    ("three_quarter", "Three-quarter view"),
    ("profile", "Profile view"),
    ("back_view", "Back view"),
    ("emotions", "Emotions"),
    ("poses", "Poses"),
    ("outfit_details", "Outfit details"),
]


CHARACTER_STATUS_CHOICES = [
    ("draft", "Draft"),
    ("active", "Active"),
    ("references_locked", "References locked"),
]


ASSET_STATUS_CHOICES = [
    ("generating", "Generating"),
    ("ready", "Ready"),
    ("failed", "Failed"),
]


def backfill_asset_status(apps, schema_editor):
    CharacterAsset = apps.get_model("w_craft_back", "CharacterAsset")
    CharacterAsset.objects.filter(status="").update(status="ready")


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0017_appearance_height_cm"),
    ]

    operations = [
        # --- Extend CharacterAssetType choices --------------------------------
        migrations.AlterField(
            model_name="characterasset",
            name="asset_type",
            field=models.CharField(choices=ASSET_TYPE_CHOICES, max_length=64),
        ),
        # --- Extend CharacterImageType: drop old CHECK constraint, alter
        #     field, then re-add CHECK with the extended set ------------------
        migrations.RemoveConstraint(
            model_name="characterimage",
            name="chk_character_image_type",
        ),
        migrations.AlterField(
            model_name="characterimage",
            name="image_type",
            field=models.CharField(choices=IMAGE_TYPE_CHOICES, max_length=32),
        ),
        migrations.AddConstraint(
            model_name="characterimage",
            constraint=models.CheckConstraint(
                check=Q(image_type__in=[value for value, _ in IMAGE_TYPE_CHOICES]),
                name="chk_character_image_type",
            ),
        ),
        # --- Extend CharacterStatus choices -----------------------------------
        migrations.AlterField(
            model_name="studiocharacter",
            name="status",
            field=models.CharField(
                choices=CHARACTER_STATUS_CHOICES,
                default="draft",
                max_length=20,
                db_index=True,
            ),
        ),
        # --- Add references_state JSONField on character ----------------------
        migrations.AddField(
            model_name="studiocharacter",
            name="references_state",
            field=models.JSONField(blank=True, default=dict),
        ),
        # --- Add status / version / error_message / correction_prompt /
        #     updated_at on CharacterAsset ---------------------------------
        migrations.AddField(
            model_name="characterasset",
            name="status",
            field=models.CharField(
                choices=ASSET_STATUS_CHOICES,
                default="ready",
                max_length=32,
                db_index=True,
            ),
        ),
        migrations.AddField(
            model_name="characterasset",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="characterasset",
            name="error_message",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="characterasset",
            name="correction_prompt",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="characterasset",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
        # --- Backfill: any existing rows are already ready (default applies) -
        migrations.RunPython(backfill_asset_status, migrations.RunPython.noop),
        # --- Add composite index used by the references endpoints -----------
        migrations.AddIndex(
            model_name="characterasset",
            index=models.Index(
                fields=["character", "asset_type", "status"],
                name="character_a_char_at_st_idx",
            ),
        ),
    ]
