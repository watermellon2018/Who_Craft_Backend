"""Add StudioCharacter.model3d_params — parametric state of the 3D editor.

The 3D stage stores the user's mathematical edits (morph/bone/material
parameter values keyed by zone) as a single JSON document. The frontend zone
registry is the source of truth for which parameters exist; the server only
enforces the structural contract (see services/model3d_service.py), so the
column is a plain JSONField with an empty-dict default and needs no data
migration.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0029_resync_portrait_to_canonical"),
    ]

    operations = [
        migrations.AddField(
            model_name="studiocharacter",
            name="model3d_params",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
