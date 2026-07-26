"""Project access helpers.

Thin compatibility layer over the centralized :mod:`policy` module. Existing
call sites (dashboard_views, poster facade, character_studio) import these
names; they now delegate to ``policy`` so there is a single source of truth for
the role/permission matrix. Prefer importing from ``policy`` directly in new
code.
"""

from __future__ import annotations

from typing import Optional

from django.contrib.auth.models import User

from w_craft_back.movie.project import policy
from w_craft_back.movie.project.models import Project


def user_is_project_owner(user: User, project: Project) -> bool:
    return policy.is_owner(user, project)


def user_has_project_access(user: User, project: Project) -> bool:
    return policy.can_view(user, project)


def user_can_edit_project(user: User, project: Project) -> bool:
    """True for owner/admin/editor (anyone who may edit project content)."""
    return policy.can_edit(user, project)


def user_role(user: Optional[User], project: Project) -> Optional[str]:
    return policy.get_role(user, project)
