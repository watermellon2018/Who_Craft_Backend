"""Replace plaintext UserKey UUIDs with expiring, revocable token digests."""

import datetime
import hashlib

from django.db import migrations, models
import django.utils.timezone


LEGACY_ACCESS_GRACE = datetime.timedelta(days=7)


def digest_legacy_user_keys(apps, schema_editor):
    UserKey = apps.get_model("w_craft_back", "UserKey")
    now = django.utils.timezone.now()
    for user_key in UserKey.objects.all().iterator():
        raw_token = str(user_key.key)
        UserKey.objects.filter(pk=user_key.pk).update(
            key_digest=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=now + LEGACY_ACCESS_GRACE,
            rotated_at=now,
        )


class Migration(migrations.Migration):
    # Commit the digest backfill before removing the plaintext column.
    atomic = False

    dependencies = [
        ("w_craft_back", "0041_enforce_single_project_owner"),
    ]

    operations = [
        migrations.AddField(
            model_name="userkey",
            name="key_digest",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="userkey",
            name="refresh_digest",
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
        migrations.AddField(
            model_name="userkey",
            name="expires_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="userkey",
            name="refresh_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userkey",
            name="revoked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userkey",
            name="created_at",
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                editable=False,
            ),
        ),
        migrations.AddField(
            model_name="userkey",
            name="rotated_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="userkey",
            name="last_used_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            digest_legacy_user_keys,
            migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="userkey",
            name="key",
        ),
        migrations.AlterField(
            model_name="userkey",
            name="key_digest",
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="userkey",
            name="expires_at",
            field=models.DateTimeField(),
        ),
    ]
