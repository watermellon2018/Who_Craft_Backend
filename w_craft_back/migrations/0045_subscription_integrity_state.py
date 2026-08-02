from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('w_craft_back', '0044_character_generation_lifecycle'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddConstraint(
                    model_name='channelsubscription',
                    constraint=models.UniqueConstraint(
                        fields=('subscriber', 'subscribed_to'),
                        condition=models.Q(deleted_at__isnull=True),
                        name='uniq_active_channel_subscription',
                    ),
                ),
            ],
        ),
    ]
