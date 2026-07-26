"""Read-side payload builders for team / membership data.

These produce plain camelCase dicts (matching the dashboard convention in
``services.py``) rather than DRF serializers, for explicit shape control. Write
validation is done with the small DRF input serializers at the bottom.
"""

from __future__ import annotations

from typing import Optional

from rest_framework import serializers

from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
    ProjectTeamRole,
)
from w_craft_back.movie.project.team_models import ProjectInvitation

try:
    from w_craft_back.profile.models import UserProfile  # type: ignore
except Exception:  # pragma: no cover
    UserProfile = None  # type: ignore


_ACCESS_ROLE_LABELS = {
    ProjectMemberRole.OWNER: "Владелец",
    ProjectMemberRole.ADMIN: "Администратор",
    ProjectMemberRole.EDITOR: "Редактор",
    ProjectMemberRole.VIEWER: "Наблюдатель",
}


def access_role_label(role: Optional[str]) -> str:
    return _ACCESS_ROLE_LABELS.get(role, "")


def _absolute(request, file_field) -> Optional[str]:
    if not file_field:
        return None
    try:
        url = file_field.url
    except Exception:
        return None
    return request.build_absolute_uri(url) if request is not None else url


def _initials(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "?"
    parts = [p for p in name.split() if p]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return name[:1].upper()


def profile_map(user_ids):
    if UserProfile is None or not user_ids:
        return {}
    return {
        p.user_id: p
        for p in UserProfile.objects.filter(user_id__in=user_ids)
    }


def member_payload(member: ProjectMember, profile, request, *, full: bool = False) -> dict:
    user = member.user
    display_name = ""
    username = user.username
    avatar_url = None
    if profile is not None:
        display_name = (
            getattr(profile, "display_name", "")
            or getattr(profile, "public_username", "")
            or ""
        )
        username = getattr(profile, "public_username", "") or username
        avatar_url = _absolute(request, getattr(profile, "avatar", None))
    if not display_name:
        display_name = user.username or f"User #{user.id}"

    data = {
        "id": member.id,
        "userId": user.id,
        "displayName": display_name,
        "username": username,
        "avatarUrl": avatar_url,
        "initials": _initials(display_name),
        "accessRole": member.role,
        "accessRoleLabel": access_role_label(member.role),
        "teamRole": member.team_role or "",
        "teamRoleLabel": member.team_role_label(),
        "customTeamRole": member.custom_team_role or "",
        "isOwner": member.role == ProjectMemberRole.OWNER,
    }
    if full:
        data["joinedAt"] = (
            member.joined_at.isoformat() if member.joined_at else
            (member.created_at.isoformat() if member.created_at else None)
        )
    return data


def members_payload(members, request, *, full: bool = False) -> list[dict]:
    members = list(members)
    profiles = profile_map([m.user_id for m in members])
    return [
        member_payload(m, profiles.get(m.user_id), request, full=full)
        for m in members
    ]


def invitation_payload(
    invitation: ProjectInvitation, request, *, include_token_url: bool = False,
    raw_token: Optional[str] = None,
) -> dict:
    """Pending-invitation payload. The secret token is NEVER included unless the
    caller explicitly passes the freshly-created raw token (creation response)."""
    invited_by = invitation.invited_by
    invited_user = invitation.invited_user
    data = {
        "id": invitation.id,
        "invitationType": invitation.invitation_type,
        "accessRole": invitation.access_role,
        "accessRoleLabel": access_role_label(invitation.access_role),
        "teamRole": invitation.team_role or "",
        "teamRoleLabel": invitation.team_role_label(),
        "status": invitation.status,
        "invitedUsername": invited_user.username if invited_user else None,
        "invitedByUsername": invited_by.username if invited_by else None,
        "createdAt": invitation.created_at.isoformat() if invitation.created_at else None,
        "expiresAt": invitation.expires_at.isoformat() if invitation.expires_at else None,
    }
    if include_token_url and raw_token:
        # Build the accept-link the inviter shares. Frontend route handles it.
        data["inviteUrl"] = request.build_absolute_uri(
            f"/invite/{raw_token}"
        ) if request is not None else f"/invite/{raw_token}"
        data["token"] = raw_token
    return data


def incoming_invitation_payload(invitation: ProjectInvitation, request) -> dict:
    """Invitation as seen by the invited user (no secret token, project context)."""
    project = invitation.project
    invited_by = invitation.invited_by
    return {
        "id": invitation.id,
        "projectId": project.id,
        "projectTitle": project.title,
        "invitationType": invitation.invitation_type,
        "accessRole": invitation.access_role,
        "accessRoleLabel": access_role_label(invitation.access_role),
        "teamRole": invitation.team_role or "",
        "teamRoleLabel": invitation.team_role_label(),
        "invitedByUsername": invited_by.username if invited_by else None,
        "createdAt": invitation.created_at.isoformat() if invitation.created_at else None,
        "expiresAt": invitation.expires_at.isoformat() if invitation.expires_at else None,
    }


def team_role_options() -> list[dict]:
    """Static list of professional roles for the invite form selects."""
    return [
        {"value": value, "label": label}
        for value, label in ProjectTeamRole.choices
    ]


# --------------------------------------------------------------------------- #
# Write-side input validation
# --------------------------------------------------------------------------- #

class UsernameInvitationSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    access_role = serializers.ChoiceField(
        choices=[
            ProjectMemberRole.ADMIN,
            ProjectMemberRole.EDITOR,
            ProjectMemberRole.VIEWER,
        ]
    )
    team_role = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default=""
    )
    custom_team_role = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )


class LinkInvitationSerializer(serializers.Serializer):
    access_role = serializers.ChoiceField(
        choices=[
            ProjectMemberRole.ADMIN,
            ProjectMemberRole.EDITOR,
            ProjectMemberRole.VIEWER,
        ]
    )
    team_role = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default=""
    )
    custom_team_role = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )


class AccessRoleSerializer(serializers.Serializer):
    access_role = serializers.ChoiceField(
        choices=[
            ProjectMemberRole.ADMIN,
            ProjectMemberRole.EDITOR,
            ProjectMemberRole.VIEWER,
        ]
    )


class TeamRoleSerializer(serializers.Serializer):
    team_role = serializers.CharField(
        max_length=32, required=False, allow_blank=True, default=""
    )
    custom_team_role = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )


class TransferOwnershipSerializer(serializers.Serializer):
    member_id = serializers.IntegerField(min_value=1)
