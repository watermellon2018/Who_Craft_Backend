"""Make Project.owner the sole ownership authority and preserve creator attribution."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def normalize_project_owners(apps, schema_editor):
    Project = apps.get_model("w_craft_back", "Project")
    ProjectMember = apps.get_model("w_craft_back", "ProjectMember")
    UserKey = apps.get_model("w_craft_back", "UserKey")

    unresolved = []
    for project in Project.objects.all().iterator():
        owner_id = project.owner_id
        if owner_id is None and project.user_id is not None:
            owner_id = (
                UserKey.objects.filter(pk=project.user_id)
                .values_list("user_id", flat=True)
                .first()
            )
        if owner_id is None:
            unresolved.append(project.pk)
            continue

        if project.owner_id != owner_id:
            Project.objects.filter(pk=project.pk).update(owner_id=owner_id)

        ProjectMember.objects.filter(
            project_id=project.pk,
            role="owner",
        ).exclude(user_id=owner_id).update(role="admin")

        owner_member, _ = ProjectMember.objects.get_or_create(
            project_id=project.pk,
            user_id=owner_id,
            defaults={
                "role": "owner",
                "joined_at": project.created_at,
            },
        )
        update_fields = []
        if owner_member.role != "owner":
            owner_member.role = "owner"
            update_fields.append("role")
        if owner_member.joined_at is None:
            owner_member.joined_at = owner_member.created_at or project.created_at
            update_fields.append("joined_at")
        if update_fields:
            owner_member.save(update_fields=update_fields)

    if unresolved:
        raise RuntimeError(
            "Cannot enforce Project.owner: projects without a resolvable owner: "
            + ", ".join(str(project_id) for project_id in unresolved)
        )

    invalid = []
    for project in Project.objects.all().iterator():
        owner_ids = list(
            ProjectMember.objects.filter(
                project_id=project.pk,
                role="owner",
            ).values_list("user_id", flat=True)
        )
        if owner_ids != [project.owner_id]:
            invalid.append(project.pk)
    if invalid:
        raise RuntimeError(
            "Project ownership normalization failed for projects: "
            + ", ".join(str(project_id) for project_id in invalid)
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    # PostgreSQL cannot ALTER these FKs while deferred trigger events from the
    # data normalization remain pending. RunPython still gets its own atomic
    # transaction; its successful commit must precede the schema operations.
    atomic = False

    dependencies = [
        ("w_craft_back", "0040_poster_job_error_http_status"),
    ]

    operations = [
        migrations.RunPython(normalize_project_owners, noop_reverse),
        migrations.AlterField(
            model_name="project",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="w_craft_back.userkey",
            ),
        ),
        migrations.AlterField(
            model_name="project",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="owned_projects",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="projectmember",
            constraint=models.UniqueConstraint(
                condition=models.Q(("role", "owner")),
                fields=("project",),
                name="uniq_active_owner_per_project",
            ),
        ),
    ]
