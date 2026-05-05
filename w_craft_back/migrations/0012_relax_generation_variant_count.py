from django.db import migrations, models
from django.db.models import Q


def normalize_generation_variant_counts(apps, schema_editor):
    CharacterGenerationJob = apps.get_model("w_craft_back", "CharacterGenerationJob")
    CharacterGenerationJob.objects.exclude(variant_count__in=[1, 2, 4]).update(variant_count=4)


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0011_alter_characterasset_asset_type"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="charactergenerationjob",
            name="chk_generation_variant_count",
        ),
        migrations.RunPython(normalize_generation_variant_counts, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="charactergenerationjob",
            constraint=models.CheckConstraint(
                check=Q(("variant_count__in", [1, 2, 4])),
                name="chk_generation_variant_count",
            ),
        ),
    ]
