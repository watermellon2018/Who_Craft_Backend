"""Initial schema for project poster generation.

Adds three tables: ProjectPoster (one-to-one with Project), PosterGenerationJob
(one row per generate-click), PosterVariant (one row per produced image). The
``selected_variant`` FK on ProjectPoster is created in this same migration —
Django's deferred constraint handling resolves the cycle automatically.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('w_craft_back', '0025_alter_projectactivity_activity_type'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ProjectPoster',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(
                    choices=[
                        ('empty', 'Нет постера'),
                        ('generating', 'Генерируется'),
                        ('ready', 'Готов'),
                        ('failed', 'Ошибка'),
                    ],
                    default='empty',
                    max_length=20,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='poster',
                    to='w_craft_back.project',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='project_posters',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
        migrations.CreateModel(
            name='PosterGenerationJob',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('prompt', models.TextField()),
                ('negative_prompt', models.TextField(blank=True, default='')),
                ('style', models.CharField(
                    choices=[
                        ('cinematic', 'Кинематографичный'),
                        ('anime', 'Аниме'),
                        ('dark_fantasy', 'Тёмное фэнтези'),
                        ('realism', 'Реализм'),
                    ],
                    max_length=64,
                )),
                ('format', models.CharField(
                    choices=[
                        ('vertical', 'Вертикальный'),
                        ('square', 'Квадратный'),
                        ('horizontal', 'Горизонтальный'),
                    ],
                    max_length=32,
                )),
                ('aspect_ratio', models.CharField(max_length=16)),
                ('width', models.PositiveIntegerField(blank=True, null=True)),
                ('height', models.PositiveIntegerField(blank=True, null=True)),
                ('reference_image_url', models.TextField(blank=True, default='')),
                ('model_provider', models.CharField(blank=True, default='', max_length=64)),
                ('model_name', models.CharField(blank=True, default='', max_length=128)),
                ('status', models.CharField(
                    choices=[
                        ('queued', 'В очереди'),
                        ('processing', 'В процессе'),
                        ('completed', 'Завершено'),
                        ('failed', 'Ошибка'),
                        ('cancelled', 'Отменено'),
                    ],
                    default='queued',
                    max_length=20,
                )),
                ('credits_cost', models.PositiveIntegerField(default=1)),
                ('error_message', models.TextField(blank=True, default='')),
                ('error_code', models.CharField(blank=True, default='', max_length=128)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('poster', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='jobs',
                    to='w_craft_back.projectposter',
                )),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='poster_jobs',
                    to='w_craft_back.project',
                )),
                ('reference_asset', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='poster_jobs',
                    to='w_craft_back.projectasset',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='poster_generation_jobs',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='PosterVariant',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.ImageField(upload_to='projects/posters/variants/')),
                ('thumbnail', models.ImageField(blank=True, null=True, upload_to='projects/posters/thumbnails/')),
                ('image_url', models.TextField(blank=True, default='')),
                ('thumbnail_url', models.TextField(blank=True, default='')),
                ('variant_index', models.PositiveSmallIntegerField(default=0)),
                ('width', models.PositiveIntegerField(blank=True, null=True)),
                ('height', models.PositiveIntegerField(blank=True, null=True)),
                ('file_size_bytes', models.BigIntegerField(blank=True, null=True)),
                ('mime_type', models.CharField(blank=True, default='', max_length=64)),
                ('seed', models.BigIntegerField(blank=True, null=True)),
                ('is_selected', models.BooleanField(default=False)),
                ('is_deleted', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='variants',
                    to='w_craft_back.postergenerationjob',
                )),
                ('poster', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='variants',
                    to='w_craft_back.projectposter',
                )),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='poster_variants',
                    to='w_craft_back.project',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='poster_variants',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='projectposter',
            name='selected_variant',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='selected_for',
                to='w_craft_back.postervariant',
            ),
        ),
        migrations.AddIndex(
            model_name='projectposter',
            index=models.Index(fields=['user'], name='w_craft_bac_user_id_7269d8_idx'),
        ),
        migrations.AddIndex(
            model_name='projectposter',
            index=models.Index(fields=['status'], name='w_craft_bac_status_9f40bf_idx'),
        ),
        migrations.AddIndex(
            model_name='postergenerationjob',
            index=models.Index(fields=['project', '-created_at'], name='w_craft_bac_project_012e1e_idx'),
        ),
        migrations.AddIndex(
            model_name='postergenerationjob',
            index=models.Index(fields=['user', 'status'], name='w_craft_bac_user_id_b74683_idx'),
        ),
        migrations.AddIndex(
            model_name='postergenerationjob',
            index=models.Index(fields=['status', 'created_at'], name='w_craft_bac_status_0a6535_idx'),
        ),
        migrations.AddIndex(
            model_name='postergenerationjob',
            index=models.Index(fields=['poster', '-created_at'], name='w_craft_bac_poster__1404e4_idx'),
        ),
        migrations.AddIndex(
            model_name='postervariant',
            index=models.Index(fields=['project', '-created_at'], name='w_craft_bac_project_df06f4_idx'),
        ),
        migrations.AddIndex(
            model_name='postervariant',
            index=models.Index(fields=['poster', '-created_at'], name='w_craft_bac_poster__4b518e_idx'),
        ),
        migrations.AddIndex(
            model_name='postervariant',
            index=models.Index(fields=['job'], name='w_craft_bac_job_id_51bf0c_idx'),
        ),
        migrations.AddIndex(
            model_name='postervariant',
            index=models.Index(
                condition=models.Q(('is_selected', True)),
                fields=['project'],
                name='poster_variant_selected_idx',
            ),
        ),
    ]
