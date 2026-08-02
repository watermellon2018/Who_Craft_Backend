from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0045_subscription_integrity_state"),
    ]

    operations = [
        migrations.CreateModel(
            name="CharacterGenerationGuard",
            fields=[
                ("key", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "character_generation_guards"},
        ),
    ]
