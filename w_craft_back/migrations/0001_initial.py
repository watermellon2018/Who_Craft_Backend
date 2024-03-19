# Generated by Django 4.1 on 2024-03-16 19:36

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import mptt.fields
import uuid
import w_craft_back.characters.creating.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Audience',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('translit', models.CharField(default='', max_length=100)),
            ],
        ),
        migrations.CreateModel(
            name='Character',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('photo', models.ImageField(upload_to='project/hero/promo/')),
                ('first_name', models.CharField(default='', max_length=100)),
                ('last_name', models.CharField(default='', max_length=100)),
                ('middle_name', models.CharField(default='', max_length=100)),
                ('birth_date', models.CharField(default='', max_length=25, validators=[w_craft_back.characters.creating.models.validate_birth_date])),
                ('birth_place', models.CharField(default='', max_length=100)),
            ],
        ),
        migrations.CreateModel(
            name='Genre',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('translit', models.CharField(default='', max_length=100)),
            ],
        ),
        migrations.CreateModel(
            name='MenuFolder',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('name', models.CharField(max_length=100)),
                ('is_folder', models.BooleanField(default=False)),
                ('lft', models.PositiveIntegerField(editable=False)),
                ('rght', models.PositiveIntegerField(editable=False)),
                ('tree_id', models.PositiveIntegerField(db_index=True, editable=False)),
                ('level', models.PositiveIntegerField(editable=False)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='ItemFolder',
            fields=[
                ('menufolder_ptr', models.OneToOneField(auto_created=True, on_delete=django.db.models.deletion.CASCADE, parent_link=True, primary_key=True, serialize=False, to='w_craft_back.menufolder')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ],
            options={
                'abstract': False,
            },
            bases=('w_craft_back.menufolder',),
        ),
        migrations.CreateModel(
            name='UserKey',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='TalentsAbilities',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('talents', models.TextField(default='')),
                ('intellectual_abilities', models.TextField(default='')),
                ('physical_characteristics', models.TextField(default='')),
                ('external_characteristics', models.TextField(default='')),
                ('speech_patterns', models.TextField(default='')),
                ('character', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='w_craft_back.character')),
            ],
        ),
        migrations.CreateModel(
            name='Project',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=255)),
                ('image', models.ImageField(upload_to='project/poster/')),
                ('format', models.CharField(max_length=255)),
                ('annot', models.TextField()),
                ('desc', models.TextField()),
                ('audience', models.ManyToManyField(to='w_craft_back.audience')),
                ('genre', models.ManyToManyField(to='w_craft_back.genre')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='w_craft_back.userkey')),
            ],
        ),
        migrations.CreateModel(
            name='ProfessionHobbies',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('profession', models.CharField(default='', max_length=100)),
                ('hobbies', models.TextField(default='')),
                ('character', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='w_craft_back.character')),
            ],
        ),
        migrations.CreateModel(
            name='PersonalityTraits',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('character_type', models.TextField(default='')),
                ('personal_traits', models.TextField(default='')),
                ('strengths_weaknesses', models.TextField(default='')),
                ('complexes', models.TextField(default='')),
                ('inner_conflicts', models.TextField(default='')),
                ('individual_style', models.TextField(default='')),
                ('character', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='w_craft_back.character')),
            ],
        ),
        migrations.AddField(
            model_name='menufolder',
            name='cur_project',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='w_craft_back.project'),
        ),
        migrations.AddField(
            model_name='menufolder',
            name='parent',
            field=mptt.fields.TreeForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='children', to='w_craft_back.menufolder'),
        ),
        migrations.AddField(
            model_name='menufolder',
            name='user',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='w_craft_back.userkey'),
        ),
        migrations.CreateModel(
            name='GoalsMotivation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('purpose_in_story', models.TextField(default='')),
                ('goal', models.TextField(default='')),
                ('life_philosophy', models.TextField(default='')),
                ('character_development', models.TextField(default='')),
                ('character', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='w_craft_back.character')),
            ],
        ),
        migrations.AddField(
            model_name='character',
            name='project',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='w_craft_back.project'),
        ),
        migrations.CreateModel(
            name='BiographyRelationships',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('biography', models.TextField(default='')),
                ('relationships_with_others', models.TextField(default='')),
                ('addit_info', models.TextField(default='')),
                ('character', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='w_craft_back.character')),
            ],
        ),
    ]
