import uuid

import django.utils.timezone
from django.db import migrations, models


def deduplicate_user_keys(apps, schema_editor):
    UserKey = apps.get_model("w_craft_back", "UserKey")
    seen_keys = set()

    for user_key in UserKey.objects.order_by("id"):
        if user_key.key not in seen_keys:
            seen_keys.add(user_key.key)
            continue

        new_key = uuid.uuid4()
        while UserKey.objects.filter(key=new_key).exists():
            new_key = uuid.uuid4()

        user_key.key = new_key
        user_key.save(update_fields=["key"])
        seen_keys.add(new_key)


def normalize_generation_variant_counts(apps, schema_editor):
    CharacterGenerationJob = apps.get_model("w_craft_back", "CharacterGenerationJob")
    CharacterGenerationJob.objects.exclude(variant_count__in=[1, 2, 4]).update(variant_count=4)


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0006_character_studio"),
    ]

    operations = [
        migrations.RunPython(deduplicate_user_keys, migrations.RunPython.noop),
        migrations.RunPython(normalize_generation_variant_counts, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="userkey",
            name="key",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="genre",
            name="translit",
            field=models.CharField(default="", max_length=100, unique=True),
        ),
        migrations.AlterField(
            model_name="audience",
            name="translit",
            field=models.CharField(default="", max_length=100, unique=True),
        ),
        migrations.AlterField(
            model_name="project",
            name="image",
            field=models.ImageField(blank=True, default="", upload_to="project/poster/"),
        ),
        migrations.AlterField(
            model_name="character",
            name="photo",
            field=models.ImageField(blank=True, default="", upload_to="project/hero/promo/"),
        ),
        migrations.AddField(
            model_name="character",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="character",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="menufolder",
            name="key",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="relationshiptype",
            name="translit",
            field=models.CharField(max_length=255, unique=True),
        ),
        migrations.AddConstraint(
            model_name="graphedge",
            constraint=models.UniqueConstraint(
                fields=("user", "project", "from_node", "to_node"),
                name="uniq_graph_edge_direction",
            ),
        ),
        migrations.AddConstraint(
            model_name="studiocharacter",
            constraint=models.CheckConstraint(
                check=models.Q(("age__isnull", True))
                | (models.Q(("age__gte", 0)) & models.Q(("age__lte", 130))),
                name="chk_studio_character_age_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="characteroutfit",
            constraint=models.UniqueConstraint(
                condition=models.Q(("archived_at__isnull", True), ("is_default", True)),
                fields=("character",),
                name="uniq_default_active_outfit",
            ),
        ),
        migrations.AddConstraint(
            model_name="characterversion",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_default", True)),
                fields=("character",),
                name="uniq_default_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="characterversion",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("character",),
                name="uniq_active_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="characterasset",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_primary", True)),
                fields=("character",),
                name="uniq_primary_asset",
            ),
        ),
        migrations.AddConstraint(
            model_name="characterasset",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_canonical", True)),
                fields=("character",),
                name="uniq_canonical_asset",
            ),
        ),
        migrations.AddConstraint(
            model_name="charactergenerationjob",
            constraint=models.CheckConstraint(
                check=models.Q(("progress__gte", 0)) & models.Q(("progress__lte", 100)),
                name="chk_generation_progress_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="charactergenerationjob",
            constraint=models.CheckConstraint(
                check=models.Q(("variant_count__in", [1, 2, 4])),
                name="chk_generation_variant_count",
            ),
        ),
        migrations.AddConstraint(
            model_name="charactervariant",
            constraint=models.UniqueConstraint(
                fields=("job", "variant_index"),
                name="uniq_variant_index_per_job",
            ),
        ),
        migrations.AddConstraint(
            model_name="characterexpression",
            constraint=models.UniqueConstraint(
                fields=("character", "expression_type"),
                name="uniq_expression_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="characterexpression",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_default", True)),
                fields=("character",),
                name="uniq_default_expression",
            ),
        ),
        migrations.AddConstraint(
            model_name="characterrelationship",
            constraint=models.UniqueConstraint(
                fields=("project", "source_character", "target_character", "relation_type"),
                name="uniq_character_relationship",
            ),
        ),
    ]
