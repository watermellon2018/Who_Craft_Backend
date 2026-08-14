from django.db import migrations
from django.db.models import F, Q


def copy_project_cover_forward(apps, schema_editor) -> None:
    """Move legacy project image names without overwriting canonical covers."""

    Project = apps.get_model("w_craft_back", "Project")
    database = schema_editor.connection.alias
    projects = Project.objects.using(database)

    conflicting_ids = list(
        projects.exclude(image="")
        .exclude(cover_image__isnull=True)
        .exclude(cover_image="")
        .exclude(cover_image=F("image"))
        .values_list("id", flat=True)
    )
    if conflicting_ids:
        raise RuntimeError(
            "Project cover migration found different image and cover_image "
            f"values for project ids: {conflicting_ids}"
        )

    projects.filter(
        Q(cover_image__isnull=True) | Q(cover_image="")
    ).exclude(image="").update(cover_image=F("image"))


def copy_project_cover_reverse(apps, schema_editor) -> None:
    """Restore the legacy image name when rolling the migration back."""

    Project = apps.get_model("w_craft_back", "Project")
    database = schema_editor.connection.alias
    Project.objects.using(database).exclude(cover_image__isnull=True).exclude(
        cover_image=""
    ).update(image=F("cover_image"))


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0052_remove_legacy_character_models"),
    ]

    operations = [
        migrations.RunPython(
            copy_project_cover_forward,
            copy_project_cover_reverse,
        ),
        migrations.RemoveField(
            model_name="project",
            name="user",
        ),
        migrations.RemoveField(
            model_name="project",
            name="image",
        ),
        migrations.RenameField(
            model_name="project",
            old_name="genre",
            new_name="genres",
        ),
        migrations.RenameField(
            model_name="project",
            old_name="audience",
            new_name="audiences",
        ),
        migrations.RenameField(
            model_name="project",
            old_name="annot",
            new_name="annotation",
        ),
        migrations.RenameField(
            model_name="project",
            old_name="desc",
            new_name="synopsis",
        ),
        migrations.RenameField(
            model_name="project",
            old_name="description",
            new_name="summary",
        ),
    ]
