from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('w_craft_back', '0019_userprofile'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='userprofile',
            name='is_verified',
        ),
    ]
