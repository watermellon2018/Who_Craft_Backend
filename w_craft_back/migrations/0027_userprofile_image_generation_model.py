"""Add per-user image-generation model preference.

A blank string means "use the env/registry default" — backwards compatible
with all existing profiles, which keep working unchanged.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('w_craft_back', '0026_project_poster_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='image_generation_model',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'Registry key from MODEL_REGISTRY, e.g. "gemini-imagen-4". '
                    'Empty falls back to env/registry default.'
                ),
                max_length=64,
            ),
        ),
    ]
