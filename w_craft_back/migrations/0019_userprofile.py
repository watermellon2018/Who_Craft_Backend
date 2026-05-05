from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('w_craft_back', '0018_references_stage'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('display_name', models.CharField(blank=True, default='', max_length=255)),
                ('tagline', models.CharField(blank=True, default='', max_length=255)),
                ('bio', models.TextField(blank=True, default='')),
                ('location', models.CharField(blank=True, default='', max_length=255)),
                ('avatar', models.ImageField(blank=True, null=True, upload_to='avatars/')),
                ('cover', models.ImageField(blank=True, null=True, upload_to='covers/')),
                ('is_verified', models.BooleanField(default=False)),
                ('language', models.CharField(default='ru', max_length=10)),
                ('private_account', models.BooleanField(default=False)),
                ('notifications_enabled', models.BooleanField(default=True)),
                ('favorite_genres', models.JSONField(blank=True, default=list)),
                ('interests', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='profile',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'db_table': 'user_profiles',
            },
        ),
    ]
