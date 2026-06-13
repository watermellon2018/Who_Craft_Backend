"""Centralized project access policy.

Single source of truth for "who may do what to a project". Every project-scoped
endpoint should resolve access through this module instead of hand-rolling
``if user == project.owner`` checks.

Roles (ProjectMemberRole): owner > admin > editor > viewer.

Resolution of the current user's role unions three ownership signals (for
back-compat with the two ownership models that predate this feature):
  1. ``project.owner_id``            (direct AUTH_USER_MODEL FK)
  2. ``project.user.user_id``        (legacy UserKey wrapper)
  3. ``ProjectMember.role``          (the collaboration source of truth)

A user matched by (1) or (2) is always treated as ``owner`` even if no
ProjectMember row exists yet — the data migration backfills those rows, but we
stay correct if one is somehow missing.

The module exposes both boolean predicates (``can_edit`` etc.) and an ``Action``
enum + ``can(role, action)`` matrix so views can ask the policy directly.
"""

from __future__ import annotations

import enum
from typing import Optional

from django.contrib.auth.models import User
from django.db.models import Q

from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project


# Role ordering for "at least this role" comparisons.
_ROLE_RANK = {
    ProjectMemberRole.VIEWER: 0,
    ProjectMemberRole.EDITOR: 1,
    ProjectMemberRole.ADMIN: 2,
    ProjectMemberRole.OWNER: 3,
}


class Action(str, enum.Enum):
    """Discrete, permission-checked operations on a project."""

    VIEW = "view"
    EDIT_CONTENT = "edit_content"          # scenes, characters, music, scripts…
    RUN_GENERATION = "run_generation"
    EDIT_SETTINGS = "edit_settings"        # project settings / metadata
    PUBLISH = "publish"
    MANAGE_TEAM = "manage_team"            # invite / remove / change roles
    TRANSFER_OWNERSHIP = "transfer_ownership"
    DELETE_PROJECT = "delete_project"
    LEAVE_PROJECT = "leave_project"


# Role → allowed actions matrix. Mirrors the task's role spec exactly.
_MATRIX: dict[str, set[Action]] = {
    ProjectMemberRole.OWNER: {
        Action.VIEW,
        Action.EDIT_CONTENT,
        Action.RUN_GENERATION,
        Action.EDIT_SETTINGS,
        Action.PUBLISH,
        Action.MANAGE_TEAM,
        Action.TRANSFER_OWNERSHIP,
        Action.DELETE_PROJECT,
        # Owner cannot LEAVE_PROJECT without transferring ownership first — that
        # special-case is enforced in the leave flow, not the matrix.
    },
    ProjectMemberRole.ADMIN: {
        Action.VIEW,
        Action.EDIT_CONTENT,
        Action.RUN_GENERATION,
        Action.EDIT_SETTINGS,
        Action.PUBLISH,
        Action.MANAGE_TEAM,
        Action.LEAVE_PROJECT,
        # No DELETE_PROJECT, no TRANSFER_OWNERSHIP.
    },
    ProjectMemberRole.EDITOR: {
        Action.VIEW,
        Action.EDIT_CONTENT,
        Action.RUN_GENERATION,
        Action.LEAVE_PROJECT,
    },
    ProjectMemberRole.VIEWER: {
        Action.VIEW,
        Action.LEAVE_PROJECT,
    },
}


# --------------------------------------------------------------------------- #
# Role resolution
# --------------------------------------------------------------------------- #

def _legacy_owner_id(project: Project) -> Optional[int]:
    if not project.user_id:
        return None
    return getattr(project.user, "user_id", None)


def get_role(user: Optional[User], project: Project) -> Optional[str]:
    """Return the user's role on the project, or ``None`` if no access."""
    if user is None or not getattr(user, "id", None):
        return None
    if project.owner_id == user.id or _legacy_owner_id(project) == user.id:
        return ProjectMemberRole.OWNER
    member = ProjectMember.objects.filter(project=project, user=user).first()
    return member.role if member else None


def is_member(user: Optional[User], project: Project) -> bool:
    return get_role(user, project) is not None


def has_at_least(role: Optional[str], minimum: str) -> bool:
    if role is None:
        return False
    return _ROLE_RANK.get(role, -1) >= _ROLE_RANK.get(minimum, 99)


# --------------------------------------------------------------------------- #
# Action checks
# --------------------------------------------------------------------------- #

def role_can(role: Optional[str], action: Action) -> bool:
    if role is None:
        return False
    return action in _MATRIX.get(role, set())


def can(user: Optional[User], project: Project, action: Action) -> bool:
    return role_can(get_role(user, project), action)


# Convenience predicates used across views.
def can_view(user, project) -> bool:
    return can(user, project, Action.VIEW)


def can_edit(user, project) -> bool:
    return can(user, project, Action.EDIT_CONTENT)


def can_run_generation(user, project) -> bool:
    return can(user, project, Action.RUN_GENERATION)


def can_edit_settings(user, project) -> bool:
    return can(user, project, Action.EDIT_SETTINGS)


def can_publish(user, project) -> bool:
    return can(user, project, Action.PUBLISH)


def can_manage_team(user, project) -> bool:
    return can(user, project, Action.MANAGE_TEAM)


def can_transfer_ownership(user, project) -> bool:
    return can(user, project, Action.TRANSFER_OWNERSHIP)


def can_delete_project(user, project) -> bool:
    return can(user, project, Action.DELETE_PROJECT)


def is_owner(user, project) -> bool:
    return get_role(user, project) == ProjectMemberRole.OWNER


def can_leave_project(user, project) -> bool:
    """A non-owner member may leave. The owner may not (must transfer first)."""
    role = get_role(user, project)
    if role is None:
        return False
    return role != ProjectMemberRole.OWNER


# --------------------------------------------------------------------------- #
# Queryset scoping
# --------------------------------------------------------------------------- #

def accessible_projects_q(user: User) -> Q:
    """Q filter selecting every project the user may access (own / member)."""
    return (
        Q(owner_id=user.id)
        | Q(user__user_id=user.id)
        | Q(members__user_id=user.id)
    )


def accessible_project_ids(user: User) -> set[int]:
    if user is None or not getattr(user, "id", None):
        return set()
    return set(
        Project.objects.filter(accessible_projects_q(user))
        .values_list("id", flat=True)
        .distinct()
    )


# --------------------------------------------------------------------------- #
# Permission summary (for the frontend)
# --------------------------------------------------------------------------- #

def permission_summary(user: Optional[User], project: Project) -> dict:
    """Compact, frontend-facing capability map for the current user."""
    role = get_role(user, project)
    return {
        "currentUserRole": role,
        "canView": role_can(role, Action.VIEW),
        "canEdit": role_can(role, Action.EDIT_CONTENT),
        "canRunGeneration": role_can(role, Action.RUN_GENERATION),
        "canEditSettings": role_can(role, Action.EDIT_SETTINGS),
        "canPublish": role_can(role, Action.PUBLISH),
        "canManageTeam": role_can(role, Action.MANAGE_TEAM),
        "canTransferOwnership": role_can(role, Action.TRANSFER_OWNERSHIP),
        "canDeleteProject": role_can(role, Action.DELETE_PROJECT),
        "canLeaveProject": can_leave_project(user, project),
    }
