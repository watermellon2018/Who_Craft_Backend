from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0059_project_credit_budgets_and_admin_transfer"),
    ]

    operations = [
        migrations.AddField(
            model_name="musicgenerationjob",
            name="provider_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
