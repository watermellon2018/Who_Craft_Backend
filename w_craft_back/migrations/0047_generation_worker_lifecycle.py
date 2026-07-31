import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0046_character_generation_guard"),
    ]

    operations = [
        migrations.AlterField(
            model_name="charactergenerationjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("processing", "Processing"),
                    ("cancellation_requested", "Cancellation requested"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                default="queued",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="cancellation_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="retry_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="retries",
                to="w_craft_back.charactergenerationjob",
            ),
        ),
        migrations.AlterField(
            model_name="postergenerationjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("cancellation_requested", "Cancellation requested"),
                    ("queued", "\u0412 \u043e\u0447\u0435\u0440\u0435\u0434\u0438"),
                    ("processing", "\u0412 \u043f\u0440\u043e\u0446\u0435\u0441\u0441\u0435"),
                    ("completed", "\u0417\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u043e"),
                    ("failed", "\u041e\u0448\u0438\u0431\u043a\u0430"),
                    ("cancelled", "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e"),
                ],
                default="queued",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="cancellation_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="lease_token",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="max_attempts",
            field=models.PositiveSmallIntegerField(default=3),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="progress",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="provider_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="reference_mime_type",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="reference_storage_key",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="requested_model",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="postergenerationjob",
            name="retry_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="retries",
                to="w_craft_back.postergenerationjob",
            ),
        ),
        migrations.AddConstraint(
            model_name="postergenerationjob",
            constraint=models.CheckConstraint(
                condition=models.Q(progress__gte=0, progress__lte=100),
                name="chk_poster_job_progress_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="postergenerationjob",
            constraint=models.CheckConstraint(
                condition=models.Q(attempts__lte=models.F("max_attempts")),
                name="chk_poster_job_attempts",
            ),
        ),
    ]
