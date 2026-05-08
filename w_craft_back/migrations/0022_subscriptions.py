import uuid

from django.conf import settings
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models

import w_craft_back.profile.models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('w_craft_back', '0021_profile_edit_schema'),
    ]

    operations = [
        # 1) Enable pg_trgm extension
        TrigramExtension(),

        # 2) UserProfile: tighten public_username (max_length 32 -> 30, new validator)
        migrations.AlterField(
            model_name='userprofile',
            name='public_username',
            field=models.CharField(
                blank=True,
                max_length=30,
                null=True,
                unique=True,
                validators=[w_craft_back.profile.models.validate_public_username],
            ),
        ),

        # 3) UserProfile: subscriber/subscription counters
        migrations.AddField(
            model_name='userprofile',
            name='subscribers_count',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='subscriptions_count',
            field=models.IntegerField(default=0),
        ),

        # 4) Trigram + lower-unique indexes on UserProfile.public_username,
        #    plus a trigram index on display_name for similarity ranking.
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_user_profiles_public_username_lower "
                "ON user_profiles (lower(public_username)) "
                "WHERE public_username IS NOT NULL;"
            ),
            reverse_sql="DROP INDEX IF EXISTS uniq_user_profiles_public_username_lower;",
        ),
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_public_username_trgm "
                "ON user_profiles USING gin (public_username gin_trgm_ops);"
            ),
            reverse_sql="DROP INDEX IF EXISTS idx_user_profiles_public_username_trgm;",
        ),
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_display_name_trgm "
                "ON user_profiles USING gin (display_name gin_trgm_ops);"
            ),
            reverse_sql="DROP INDEX IF EXISTS idx_user_profiles_display_name_trgm;",
        ),

        # 5) ChannelSubscription model
        migrations.CreateModel(
            name='ChannelSubscription',
            fields=[
                ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ('notifications_enabled', models.BooleanField(default=True)),
                ('is_favorite', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('subscriber', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='subscriptions_made',
                    db_column='subscriber_user_id',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('subscribed_to', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='subscribers_relation',
                    db_column='subscribed_to_user_id',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'db_table': 'channel_subscriptions',
            },
        ),
        migrations.AddConstraint(
            model_name='channelsubscription',
            constraint=models.CheckConstraint(
                check=~models.Q(subscriber=models.F('subscribed_to')),
                name='channel_subscriptions_no_self',
            ),
        ),

        # 6) Indexes for channel_subscriptions
        migrations.RunSQL(
            sql=(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_channel_subscription "
                "ON channel_subscriptions (subscriber_user_id, subscribed_to_user_id) "
                "WHERE deleted_at IS NULL;"
            ),
            reverse_sql="DROP INDEX IF EXISTS uniq_active_channel_subscription;",
        ),
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_channel_subscriptions_subscriber_active "
                "ON channel_subscriptions (subscriber_user_id, deleted_at);"
            ),
            reverse_sql="DROP INDEX IF EXISTS idx_channel_subscriptions_subscriber_active;",
        ),
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_channel_subscriptions_target_active "
                "ON channel_subscriptions (subscribed_to_user_id, deleted_at);"
            ),
            reverse_sql="DROP INDEX IF EXISTS idx_channel_subscriptions_target_active;",
        ),
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_channel_subscriptions_favorites_first "
                "ON channel_subscriptions (subscriber_user_id, is_favorite DESC, created_at DESC) "
                "WHERE deleted_at IS NULL;"
            ),
            reverse_sql="DROP INDEX IF EXISTS idx_channel_subscriptions_favorites_first;",
        ),
    ]
