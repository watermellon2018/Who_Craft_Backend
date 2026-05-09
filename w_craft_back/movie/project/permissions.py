"""Project access helpers.

Resolution order is unified:
1. ``user`` is a Django ``User`` (resolved upstream from ``token_user`` -> UserKey).
2. Project ownership: legacy ``project.user.user_id`` (UserKey wrapper) and
   the newer ``project.owner_id`` (direct AUTH_USER_MODEL FK) are both honored.
3. Membership: ``ProjectMember`` with role owner/editor/viewer.
"""

from __future__ import annotations

from typing import Optional

from django.contrib.auth.models import User

from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project


def user_is_project_owner(user: User, project: Project) -> bool:
    if project.owner_id == user.id:
        return True
    legacy_owner_id = getattr(project.user, "user_id", None) if project.user_id else None
    if legacy_owner_id == user.id:
        return True
    return ProjectMember.objects.filter(
        project=project, user=user, role=ProjectMemberRole.OWNER
    ).exists()


def _get_member_role(user: User, project: Project) -> Optional[str]:
    member = ProjectMember.objects.filter(project=project, user=user).first()
    return member.role if member else None


def user_has_project_access(user: User, project: Project) -> bool:
    if user_is_project_owner(user, project):
        return True
    return _get_member_role(user, project) is not None


def user_can_edit_project(user: User, project: Project) -> bool:
    if user_is_project_owner(user, project):
        return True
    role = _get_member_role(user, project)
    return role in (ProjectMemberRole.OWNER, ProjectMemberRole.EDITOR)
