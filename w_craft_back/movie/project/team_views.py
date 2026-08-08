"""Team-collaboration API views.

Auth and access follow the dashboard pattern: ``_resolve_user`` (X-User-Token →
UserKey → Django User) + the central :mod:`policy`. Team operations are
delegated to :mod:`team_service`, which raises ``TeamError`` subclasses that we
translate into structured ``{"code", "detail"}`` JSON responses.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.movie.project import policy, team_service
from w_craft_back.movie.project import team_errors as errors
from w_craft_back.movie.project.dashboard_models import ProjectMemberRole
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.team_serializers import (
    AccessRoleSerializer,
    LinkInvitationSerializer,
    TeamRoleSerializer,
    TransferOwnershipSerializer,
    UsernameInvitationSerializer,
    access_role_label,
    incoming_invitation_payload,
    invitation_payload,
    members_payload,
    team_role_options,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _resolve_user(request) -> Optional[User]:
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user
    return None


def _unauthorized():
    return Response({"detail": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)


def _forbidden():
    return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)


def _error_response(exc: errors.TeamError):
    return Response(exc.to_dict(), status=exc.status)


def _validation_error(serializer_errors):
    return Response(
        {"code": "VALIDATION_ERROR", "detail": "validation error", "errors": serializer_errors},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _get_project_or_404(project_id) -> Project:
    return get_object_or_404(
        Project.objects.select_related("owner", "user"), pk=project_id
    )


class _TeamView(APIView):
    """Base resolving the user + project and enforcing at least view access."""

    def _ctx(self, request, project_id, *, require_view=True):
        user = _resolve_user(request)
        if user is None:
            return None, None, _unauthorized()
        project = _get_project_or_404(project_id)
        if require_view and not policy.can_view(user, project):
            # Project policy hides nonexistence of access via 403 (the caller is
            # authenticated; they simply lack access).
            return None, None, _forbidden()
        return user, project, None


# --------------------------------------------------------------------------- #
# Team summary + members
# --------------------------------------------------------------------------- #

class ProjectTeamView(_TeamView):
    """GET compact team summary + permission flags for the current user."""

    def get(self, request, project_id: int):
        user, project, err = self._ctx(request, project_id)
        if err:
            return err

        members = team_service.list_members(project)
        owner = next(
            (m for m in members if m.role == ProjectMemberRole.OWNER), None
        )
        perms = policy.permission_summary(user, project)
        member_dicts = members_payload(members, request, full=False)

        return Response(
            {
                "projectId": project.id,
                "projectTitle": project.title,
                "memberCount": len(member_dicts),
                "members": member_dicts,
                "ownerUserId": owner.user_id if owner else None,
                "ownerName": next(
                    (m["displayName"] for m in member_dicts if m["isOwner"]), None
                ),
                "currentUserRole": perms["currentUserRole"],
                "currentUserRoleLabel": access_role_label(perms["currentUserRole"]),
                "permissions": perms,
                "teamRoleOptions": team_role_options(),
            }
        )


class ProjectMembersView(_TeamView):
    """GET full member list (for the dedicated team page)."""

    def get(self, request, project_id: int):
        user, project, err = self._ctx(request, project_id)
        if err:
            return err
        members = team_service.list_members(project)
        return Response(
            {
                "members": members_payload(members, request, full=True),
                "memberCount": members.count(),
                "permissions": policy.permission_summary(user, project),
            }
        )


class ProjectMemberDetailView(_TeamView):
    """PATCH access/team role, DELETE remove member."""

    def patch(self, request, project_id: int, member_id: int):
        user, project, err = self._ctx(request, project_id)
        if err:
            return err
        # Two sub-operations distinguished by which field is present.
        data = request.data if isinstance(request.data, dict) else {}
        try:
            if "access_role" in data:
                ser = AccessRoleSerializer(data=data)
                if not ser.is_valid():
                    return _validation_error(ser.errors)
                member = team_service.change_member_access_role(
                    user, project, member_id, ser.validated_data["access_role"]
                )
            elif "team_role" in data or "custom_team_role" in data:
                ser = TeamRoleSerializer(data=data)
                if not ser.is_valid():
                    return _validation_error(ser.errors)
                member = team_service.change_member_team_role(
                    user,
                    project,
                    member_id,
                    ser.validated_data.get("team_role", ""),
                    ser.validated_data.get("custom_team_role", ""),
                )
            else:
                return _validation_error({"detail": ["nothing to update"]})
        except errors.TeamError as exc:
            return _error_response(exc)

        from w_craft_back.movie.project.team_serializers import (
            member_payload,
            profile_map,
        )

        prof = profile_map([member.user_id]).get(member.user_id)
        return Response(member_payload(member, prof, request, full=True))

    def delete(self, request, project_id: int, member_id: int):
        user, project, err = self._ctx(request, project_id)
        if err:
            return err
        try:
            team_service.remove_member(user, project, member_id)
        except errors.TeamError as exc:
            return _error_response(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectLeaveView(_TeamView):
    """POST — current user leaves the project."""

    def post(self, request, project_id: int):
        user, project, err = self._ctx(request, project_id)
        if err:
            return err
        try:
            team_service.leave_project(user, project)
        except errors.TeamError as exc:
            return _error_response(exc)
        return Response({"detail": "left"}, status=status.HTTP_200_OK)


class ProjectTransferOwnershipView(_TeamView):
    """POST — owner transfers ownership to another active member."""

    def post(self, request, project_id: int):
        user, project, err = self._ctx(request, project_id)
        if err:
            return err
        ser = TransferOwnershipSerializer(data=request.data)
        if not ser.is_valid():
            return _validation_error(ser.errors)
        try:
            team_service.transfer_ownership(
                user, project, ser.validated_data["member_id"]
            )
        except errors.TeamError as exc:
            return _error_response(exc)
        return Response({"detail": "transferred"}, status=status.HTTP_200_OK)


# --------------------------------------------------------------------------- #
# Invitations (project-scoped)
# --------------------------------------------------------------------------- #

class ProjectInvitationsView(_TeamView):
    """GET pending invitations; POST create (username or link)."""

    def get(self, request, project_id: int):
        user, project, err = self._ctx(request, project_id)
        if err:
            return err
        if not policy.can_manage_team(user, project):
            return _forbidden()
        invitations = team_service.list_pending_invitations(project)
        return Response(
            {
                "invitations": [
                    invitation_payload(inv, request) for inv in invitations
                ]
            }
        )

    def post(self, request, project_id: int):
        user, project, err = self._ctx(request, project_id)
        if err:
            return err
        data = request.data if isinstance(request.data, dict) else {}
        invite_type = data.get("invitation_type") or (
            "username" if data.get("username") else "link"
        )
        try:
            if invite_type == "username":
                ser = UsernameInvitationSerializer(data=data)
                if not ser.is_valid():
                    return _validation_error(ser.errors)
                v = ser.validated_data
                invitation, raw = team_service.create_username_invitation(
                    user,
                    project,
                    username=v["username"],
                    access_role=v["access_role"],
                    team_role=v.get("team_role", ""),
                    custom_team_role=v.get("custom_team_role", ""),
                )
            else:
                ser = LinkInvitationSerializer(data=data)
                if not ser.is_valid():
                    return _validation_error(ser.errors)
                v = ser.validated_data
                invitation, raw = team_service.create_link_invitation(
                    user,
                    project,
                    access_role=v["access_role"],
                    team_role=v.get("team_role", ""),
                    custom_team_role=v.get("custom_team_role", ""),
                )
        except errors.TeamError as exc:
            return _error_response(exc)

        return Response(
            invitation_payload(
                invitation, request, include_token_url=True, raw_token=raw
            ),
            status=status.HTTP_201_CREATED,
        )


class ProjectInvitationCancelView(_TeamView):
    """DELETE — cancel a pending invitation."""

    def delete(self, request, project_id: int, invitation_id: int):
        user, project, err = self._ctx(request, project_id)
        if err:
            return err
        try:
            team_service.cancel_invitation(user, project, invitation_id)
        except errors.TeamError as exc:
            return _error_response(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Invitations (user-scoped — incoming + accept/decline)
# --------------------------------------------------------------------------- #

class IncomingInvitationsView(APIView):
    """GET — invitations addressed to the current user (by username)."""

    def get(self, request):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()
        invitations = team_service.list_incoming_invitations(user)
        return Response(
            {
                "invitations": [
                    incoming_invitation_payload(inv, request) for inv in invitations
                ]
            }
        )


class InvitationActionView(APIView):
    """POST accept / decline a username invitation by its id."""

    def post(self, request, invitation_id: int, action: str):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()
        from w_craft_back.movie.project.team_models import ProjectInvitation

        invitation = ProjectInvitation.objects.filter(pk=invitation_id).first()
        if invitation is None:
            return _error_response(errors.InvitationNotFound())
        try:
            if action == "accept":
                member = team_service.accept_invitation_obj(user, invitation)
                return Response(
                    {"detail": "accepted", "projectId": member.project_id},
                    status=status.HTTP_200_OK,
                )
            elif action == "decline":
                team_service.decline_invitation_obj(user, invitation)
                return Response({"detail": "declined"}, status=status.HTTP_200_OK)
            else:
                return _validation_error({"action": ["unknown action"]})
        except errors.TeamError as exc:
            return _error_response(exc)


class InvitationTokenView(APIView):
    """GET preview / POST accept a link invitation by its raw token."""

    def get(self, request, token: str):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()
        invitation = team_service.get_invitation_by_token(token)
        if invitation is None:
            return _error_response(errors.InvitationNotFound())
        return Response(incoming_invitation_payload(invitation, request))

    def post(self, request, token: str):
        user = _resolve_user(request)
        if user is None:
            return _unauthorized()
        try:
            member = team_service.accept_invitation(user, token)
        except errors.TeamError as exc:
            return _error_response(exc)
        return Response(
            {"detail": "accepted", "projectId": member.project_id},
            status=status.HTTP_200_OK,
        )
