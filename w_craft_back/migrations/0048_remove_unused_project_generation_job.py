from django.db import migrations


def assert_project_generation_jobs_empty(apps, schema_editor) -> None:
    """Prevent the schema drop when any environment still has rows."""
    project_generation_job = apps.get_model(
        "w_craft_back",
        "ProjectGenerationJob",
    )
    connection = schema_editor.connection
    table_name = connection.ops.quote_name(
        project_generation_job._meta.db_table
    )
    with connection.cursor() as cursor:
        cursor.execute(f"LOCK TABLE {table_name} IN ACCESS EXCLUSIVE MODE")
    database_alias = connection.alias
    row_count = project_generation_job.objects.using(database_alias).count()
    if row_count:
        raise RuntimeError(
            "Refusing to drop ProjectGenerationJob while "
            f"{row_count} row(s) still exist"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("w_craft_back", "0047_generation_worker_lifecycle"),
    ]

    operations = [
        migrations.RunPython(
            assert_project_generation_jobs_empty,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.DeleteModel(
            name="ProjectGenerationJob",
        ),
    ]
