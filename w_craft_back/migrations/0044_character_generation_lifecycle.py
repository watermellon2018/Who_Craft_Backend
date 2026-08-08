import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def backfill_generation_actors(apps, schema_editor):
    CharacterGenerationJob = apps.get_model(
        "w_craft_back",
        "CharacterGenerationJob",
    )
    db_alias = schema_editor.connection.alias
    jobs = CharacterGenerationJob.objects.using(db_alias)
    jobs.filter(
        actor_id__isnull=True,
        user_id__isnull=False,
    ).update(actor_id=models.F("user_id"))
    jobs.filter(
        status__in=["queued", "processing"],
    ).exclude(job_type="model3d_reconstruction").update(
        status="failed",
        progress=0,
        error_code="LEGACY_JOB_STATE_UNKNOWN",
        error_message=(
            "This job was in flight before durable leases were introduced. "
            "Retry with a new request to avoid an accidental duplicate call."
        ),
        failed_at=django.utils.timezone.now(),
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0043_project_aggregate_integrity"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="generation_settings",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="studiocharacter",
            name="creation_idempotency_key",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="studiocharacter",
            name="creation_request_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="requested_character_generation_jobs",
                to="w_craft_back.userkey",
            ),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="compiled_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="idempotency_key",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="lease_token",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="max_attempts",
            field=models.PositiveSmallIntegerField(default=3),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="provider_operation",
            field=models.CharField(
                blank=True,
                default="generate",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="provider_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="request_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="timeout_seconds",
            field=models.PositiveIntegerField(default=120),
        ),
        migrations.AddField(
            model_name="charactergenerationjob",
            name="updated_at",
            field=models.DateTimeField(
                auto_now=True,
                default=django.utils.timezone.now,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_generation_actors, noop_reverse),
        migrations.AddIndex(
            model_name="charactergenerationjob",
            index=models.Index(
                fields=["status", "lease_expires_at"],
                name="char_job_status_lease_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="charactergenerationjob",
            constraint=models.CheckConstraint(
                check=models.Q(attempts__lte=models.F("max_attempts")),
                name="chk_generation_attempts",
            ),
        ),
        migrations.AddConstraint(
            model_name="studiocharacter",
            constraint=models.UniqueConstraint(
                condition=~models.Q(creation_idempotency_key=""),
                fields=("project", "user", "creation_idempotency_key"),
                name="uniq_char_create_idempotency",
            ),
        ),
        migrations.AddConstraint(
            model_name="charactergenerationjob",
            constraint=models.UniqueConstraint(
                condition=~models.Q(idempotency_key=""),
                fields=("project", "actor", "idempotency_key"),
                name="uniq_char_job_idempotency",
            ),
        ),
    ]
