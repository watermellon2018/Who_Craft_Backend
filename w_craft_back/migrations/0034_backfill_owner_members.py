"""Data migration: ensure every existing project has exactly one owner member.

For each Project, resolve the canonical owner:
  1. ``project.owner`` (direct AUTH_USER_MODEL FK), else
  2. ``project.user`` (legacy UserKey wrapper) -> its ``.user``.

Then:
  - guarantee a ProjectMember row for that user with role ``owner``
    (promoting an existing non-owner membership rather than duplicating);
  - backfill ``joined_at`` for any member that lacks it (use created_at).

Idempotent and reversible (the reverse is a no-op: we never delete data on
rollback, since we can't tell auto-created owners from pre-existing ones).
"""

from __future__ import annotations

from django.db import migrations


def backfill_owner_members(apps, schema_editor):
    Project = apps.get_model("w_craft_back", "Project")
    ProjectMember = apps.get_model("w_craft_back", "ProjectMember")

    OWNER = "owner"

    for project in Project.objects.all().iterator():
        owner_user_id = project.owner_id
        if owner_user_id is None and project.user_id is not None:
            # Legacy UserKey wrapper -> underlying auth user id.
            owner_user_id = getattr(project.user, "user_id", None)
        if owner_user_id is None:
            # Orphan project with no resolvable owner — skip rather than guess.
            continue

        member = ProjectMember.objects.filter(
            project=project, user_id=owner_user_id
        ).first()
        if member is None:
            ProjectMember.objects.create(
                project=project,
                user_id=owner_user_id,
                role=OWNER,
                joined_at=project.created_at,
            )
        elif member.role != OWNER:
            member.role = OWNER
            if member.joined_at is None:
                member.joined_at = member.created_at or project.created_at
            member.save(update_fields=["role", "joined_at"])

    # Backfill joined_at for every remaining member that lacks it.
    for member in ProjectMember.objects.filter(joined_at__isnull=True).iterator():
        member.joined_at = member.created_at
        member.save(update_fields=["joined_at"])


def noop_reverse(apps, schema_editor):
    # We deliberately do not remove backfilled owners on reverse: there is no
    # safe way to distinguish auto-created rows from genuine ones.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("w_craft_back", "0033_team_collaboration_schema"),
    ]

    operations = [
        migrations.RunPython(backfill_owner_members, noop_reverse),
    ]
