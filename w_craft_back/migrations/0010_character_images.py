import uuid

from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0009_character_entity_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="CharacterImage",
            fields=[
                ("image_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("image_type", models.CharField(choices=[("portrait", "Portrait"), ("full_body", "Full body"), ("scene", "Scene"), ("reference_sheet", "Reference sheet")], max_length=32)),
                ("image_url", models.TextField(blank=True, default="")),
                ("storage_path", models.TextField(blank=True, default="")),
                ("prompt", models.TextField(blank=True, default="")),
                ("seed", models.BigIntegerField(blank=True, null=True)),
                ("generation_params", models.JSONField(blank=True, default=dict)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("asset", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="character_images", to="w_craft_back.characterasset")),
                ("character", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="images", to="w_craft_back.studiocharacter")),
            ],
            options={
                "db_table": "character_images",
            },
        ),
        migrations.AddConstraint(
            model_name="characterimage",
            constraint=models.UniqueConstraint(condition=Q(("is_active", True)), fields=("character", "image_type"), name="uniq_active_character_image_type"),
        ),
        migrations.AddConstraint(
            model_name="characterimage",
            constraint=models.CheckConstraint(check=Q(("image_type__in", ["portrait", "full_body", "scene", "reference_sheet"])), name="chk_character_image_type"),
        ),
        migrations.AddIndex(
            model_name="characterimage",
            index=models.Index(fields=["character"], name="character_i_charact_72dd68_idx"),
        ),
        migrations.AddIndex(
            model_name="characterimage",
            index=models.Index(fields=["image_type"], name="character_i_type_055e38_idx"),
        ),
        migrations.AddIndex(
            model_name="characterimage",
            index=models.Index(fields=["character", "image_type"], name="character_i_char_ty_6ce70d_idx"),
        ),
    ]
