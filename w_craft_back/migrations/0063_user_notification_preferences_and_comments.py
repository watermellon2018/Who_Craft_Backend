# Generated manually for issue 70 so the profile rename preserves existing data.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def copy_interface_language_to_content_language(apps, schema_editor):
    UserProfile = apps.get_model('w_craft_back', 'UserProfile')
    UserProfile.objects.filter(language='en').update(content_language='en')
    UserProfile.objects.filter(language='ru').update(content_language='ru')


def reset_content_language(apps, schema_editor):
    UserProfile = apps.get_model('w_craft_back', 'UserProfile')
    UserProfile.objects.update(content_language='ru')


class Migration(migrations.Migration):
    dependencies = [
        ('w_craft_back', '0062_project_progress_sources'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name='userprofile',
            old_name='notifications_enabled',
            new_name='notifications_in_app',
        ),
        migrations.AddField(
            model_name='userprofile',
            name='content_language',
            field=models.CharField(
                choices=[('ru', 'Russian'), ('en', 'English')],
                default='ru',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='notifications_email',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='comment_permission',
            field=models.CharField(
                choices=[
                    ('everyone', 'Everyone'),
                    ('followers', 'Followers'),
                    ('nobody', 'Nobody'),
                ],
                default='everyone',
                max_length=16,
            ),
        ),
        migrations.RunPython(
            copy_interface_language_to_content_language,
            reset_content_language,
        ),
        migrations.CreateModel(
            name='NotificationDispatchReceipt',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('idempotency_key', models.CharField(max_length=255, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'db_table': 'notification_dispatch_receipts'},
        ),
        migrations.CreateModel(
            name='EmailNotificationDelivery',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('recipient_email', models.EmailField(max_length=254)),
                ('title', models.CharField(max_length=255)),
                ('message', models.TextField(blank=True, default='')),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('pending', 'Pending'),
                            ('failed', 'Failed'),
                            ('sent', 'Sent'),
                        ],
                        default='pending',
                        max_length=16,
                    ),
                ),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('last_error', models.TextField(blank=True, default='')),
                ('locked_at', models.DateTimeField(blank=True, null=True)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'dispatch_receipt',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='email_delivery',
                        to='w_craft_back.notificationdispatchreceipt',
                    ),
                ),
                (
                    'recipient',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='notification_email_deliveries',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'db_table': 'email_notification_deliveries',
                'indexes': [
                    models.Index(
                        fields=['status', 'locked_at', 'created_at'],
                        name='email_delivery_retry_idx',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='Notification',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'type',
                    models.CharField(
                        choices=[
                            ('project_invitation', 'Project invitation'),
                            ('comment', 'Comment'),
                            ('generation', 'Generation'),
                            ('system', 'System'),
                        ],
                        max_length=64,
                    ),
                ),
                ('title', models.CharField(max_length=255)),
                ('message', models.TextField(blank=True, default='')),
                ('target_url', models.CharField(blank=True, default='', max_length=1024)),
                ('entity_type', models.CharField(blank=True, default='', max_length=64)),
                ('entity_id', models.CharField(blank=True, default='', max_length=128)),
                (
                    'idempotency_key',
                    models.CharField(blank=True, max_length=255, null=True, unique=True),
                ),
                ('is_read', models.BooleanField(default=False)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'recipient',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='notifications',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'db_table': 'user_notifications',
                'ordering': ['-created_at', '-id'],
                'indexes': [
                    models.Index(
                        fields=['recipient', 'is_read', 'created_at'],
                        name='notify_recipient_read_idx',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='VideoShotComment',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('body', models.TextField(max_length=4000)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'author',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='video_shot_comments',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'shot',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='comments',
                        to='w_craft_back.videoshot',
                    ),
                ),
            ],
            options={
                'db_table': 'video_shot_comments',
                'ordering': ['created_at', 'id'],
                'indexes': [
                    models.Index(
                        fields=['shot', 'created_at'],
                        name='shot_comment_created_idx',
                    ),
                ],
            },
        ),
    ]
