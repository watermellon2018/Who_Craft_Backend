# Generated by Django 4.1 on 2024-03-17 11:23

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('w_craft_back', '0002_itemfolder_hero'),
    ]

    operations = [
        migrations.AddField(
            model_name='character',
            name='type',
            field=models.CharField(choices=[('main', 'Главный'), ('seconder', 'Второстепенный'), ('episode', 'Эпизодический')], default='seconder', max_length=50),
        ),
    ]
