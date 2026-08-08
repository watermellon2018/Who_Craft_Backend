import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0036_model3d_reconstruction_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="scene",
            name="act",
            field=models.PositiveSmallIntegerField(
                default=1,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3),
                ],
            ),
        ),
        migrations.AddField(
            model_name="scene",
            name="duration_seconds",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="scene",
            name="mood",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="scene",
            name="notes",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="scene",
            name="scene_type",
            field=models.CharField(
                choices=[
                    ("setup", "Setup"),
                    ("provocation", "Provocation"),
                    ("turn", "Turn"),
                    ("obstacle", "Obstacle"),
                    ("escalation", "Escalation"),
                    ("climax", "Climax"),
                    ("resolution", "Resolution"),
                    ("final", "Final"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="scene",
            name="script_blocks",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
