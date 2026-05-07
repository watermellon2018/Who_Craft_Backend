from django.conf import settings
from django.db import migrations, models

import w_craft_back.profile.models


DEFAULT_INTERESTS = [
    ('кино', 'kino'),
    ('анимация', 'animaciya'),
    ('AI', 'ai'),
    ('сценарии', 'scenarii'),
    ('персонажи', 'personazhi'),
    ('VFX', 'vfx'),
    ('sci-fi', 'sci-fi'),
]


def seed_interests(apps, schema_editor):
    Interest = apps.get_model('w_craft_back', 'Interest')
    for name, slug in DEFAULT_INTERESTS:
        Interest.objects.update_or_create(slug=slug, defaults={'name': name})


def unseed_interests(apps, schema_editor):
    Interest = apps.get_model('w_craft_back', 'Interest')
    Interest.objects.filter(slug__in=[slug for _, slug in DEFAULT_INTERESTS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('w_craft_back', '0020_remove_userprofile_is_verified'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserAsset',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('type', models.CharField(
                    choices=[('avatar', 'avatar'), ('cover', 'cover'), ('other', 'other')],
                    default='other',
                    max_length=16,
                )),
                ('storage_key', models.CharField(max_length=512)),
                ('url', models.URLField(blank=True, max_length=2048, null=True)),
                ('mime_type', models.CharField(blank=True, max_length=128, null=True)),
                ('size_bytes', models.BigIntegerField(blank=True, null=True)),
                ('width', models.IntegerField(blank=True, null=True)),
                ('height', models.IntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='assets',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'db_table': 'user_assets',
            },
        ),
        migrations.AddIndex(
            model_name='userasset',
            index=models.Index(fields=['user'], name='user_assets_user_idx'),
        ),
        migrations.AddIndex(
            model_name='userasset',
            index=models.Index(fields=['user', 'type'], name='user_assets_user_type_idx'),
        ),

        migrations.CreateModel(
            name='Interest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=64, unique=True)),
                ('slug', models.SlugField(max_length=80, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'interests',
                'ordering': ['name'],
            },
        ),

        migrations.CreateModel(
            name='UserInterest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('interest', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='user_interests',
                    to='w_craft_back.interest',
                )),
                ('user', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='user_interests',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'db_table': 'user_interests',
            },
        ),
        migrations.AddConstraint(
            model_name='userinterest',
            constraint=models.UniqueConstraint(fields=('user', 'interest'), name='user_interest_unique'),
        ),
        migrations.AddIndex(
            model_name='userinterest',
            index=models.Index(fields=['user'], name='user_interests_user_idx'),
        ),
        migrations.AddIndex(
            model_name='userinterest',
            index=models.Index(fields=['interest'], name='user_interests_interest_idx'),
        ),

        migrations.CreateModel(
            name='UserSocialLink',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('platform', models.CharField(
                    choices=[
                        ('telegram', 'Telegram'),
                        ('instagram', 'Instagram'),
                        ('youtube', 'YouTube'),
                        ('website', 'Website'),
                        ('tiktok', 'TikTok'),
                        ('x', 'X'),
                        ('vk', 'VK'),
                        ('other', 'Other'),
                    ],
                    max_length=32,
                )),
                ('url', models.URLField(max_length=2048)),
                ('display_order', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='social_links',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'db_table': 'user_social_links',
            },
        ),
        migrations.AddConstraint(
            model_name='usersociallink',
            constraint=models.UniqueConstraint(fields=('user', 'platform'), name='user_social_link_unique'),
        ),
        migrations.AddIndex(
            model_name='usersociallink',
            index=models.Index(fields=['user'], name='user_social_links_user_idx'),
        ),
        migrations.AddIndex(
            model_name='usersociallink',
            index=models.Index(fields=['user', 'display_order'], name='user_social_links_order_idx'),
        ),

        migrations.AddField(
            model_name='userprofile',
            name='public_username',
            field=models.CharField(
                blank=True,
                max_length=32,
                null=True,
                unique=True,
                validators=[w_craft_back.profile.models.validate_public_username],
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='avatar_asset',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name='+',
                to='w_craft_back.userasset',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='cover_asset',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name='+',
                to='w_craft_back.userasset',
            ),
        ),

        migrations.RunPython(seed_interests, reverse_code=unseed_interests),
    ]
