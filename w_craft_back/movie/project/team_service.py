"""Team-collaboration service: membership, invitations, ownership transfer.

All write paths are atomic and gated through :mod:`policy`. Every public method
takes the *acting* Django ``User`` and the ``Project`` and raises a
:class:`team_errors.TeamError` subclass (mapped to a JSON error + HTTP status by
the views) when an operation is not allowed.
"""

from __future__ import annotations

from typing import Optional

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from w_craft_back.movie.project import policy
from w_craft_back.movie.project import team_errors as errors
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
    ProjectTeamRole,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.services import record_activity
from w_craft_back.movie.project.team_models import (
    InvitationStatus,
    InvitationType,
    ProjectInvitation,
    generate_invitation_token,
    hash_invitation_token,
)


# Roles a non-owner manager (admin) may assign / change. Owner is never
# assignable via a plain role change (only via transfer_ownership).
_ASSIGNABLE_ROLES = {
    ProjectMemberRole.ADMIN,
    ProjectMemberRole.EDITOR,
    ProjectMemberRole.VIEWER,
}

_VALID_TEAM_ROLES = set(ProjectTeamRole.values)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #

def _validate_access_role(role: str) -> str:
    if role not in _ASSIGNABLE_ROLES:
        # owner is the only valid role outside the assignable set; reject it
        # explicitly so the caller gets CANNOT_ASSIGN_OWNER rather than a
        # generic invalid-role error.
        if role == ProjectMemberRole.OWNER:
            raise errors.CannotAssignOwner()
        raise errors.InvalidRole()
    return role


def _normalize_team_role(team_role: str, custom_team_role: str) -> tuple[str, str]:
    team_role = (team_role or "").strip()
    custom_team_role = (custom_team_role or "").strip()
    if team_role and team_role not in _VALID_TEAM_ROLES:
        raise errors.InvalidRole("Недопустимая профессиональная роль.")
    if team_role == ProjectTeamRole.OTHER:
        if not custom_team_role:
            raise errors.InvalidRole(
                "Укажите название роли для варианта «Другое»."
            )
    else:
        # custom label is only meaningful for OTHER; drop it otherwise.
        custom_team_role = ""
    return team_role, custom_team_role


def _require_manage(actor: User, project: Project) -> str:
    role = policy.get_role(actor, project)
    if not policy.role_can(role, policy.Action.MANAGE_TEAM):
        raise errors.InsufficientPermissions()
    return role


# --------------------------------------------------------------------------- #
# Membership queries
# --------------------------------------------------------------------------- #

def list_members(project: Project):
    return (
        ProjectMember.objects.filter(project=project)
        .select_related("user")
        .order_by("created_at", "id")
    )


def get_member(project: Project, member_id: int) -> ProjectMember:
    member = (
        ProjectMember.objects.filter(project=project, pk=member_id)
        .select_related("user")
        .first()
    )
    if member is None:
        raise errors.MemberNotFound()
    return member


def active_member_count(project: Project) -> int:
    return ProjectMember.objects.filter(project=project).count()


# --------------------------------------------------------------------------- #
# Invitations
# --------------------------------------------------------------------------- #

def _expire_stale(invitation: ProjectInvitation) -> None:
    """Flip a pending-but-expired invitation to EXPIRED in the DB lazily."""
    if invitation.status == InvitationStatus.PENDING and invitation.is_expired():
        invitation.status = InvitationStatus.EXPIRED
        invitation.save(update_fields=["status", "updated_at"])


def create_username_invitation(
    actor: User,
    project: Project,
    *,
    username: str,
    access_role: str,
    team_role: str = "",
    custom_team_role: str = "",
) -> tuple[ProjectInvitation, str]:
    """Invite an existing user by username. Returns (invitation, raw_token)."""
    _require_manage(actor, project)
    access_role = _validate_access_role(access_role)
    team_role, custom_team_role = _normalize_team_role(team_role, custom_team_role)

    username = (username or "").strip()
    if not username:
        raise errors.UserNotFound()

    invited = _resolve_user_by_username(username)
    if invited is None:
        raise errors.UserNotFound()
    if invited.id == actor.id:
        raise errors.CannotInviteSelf()
    if policy.is_member(invited, project):
        raise errors.AlreadyMember()

    # Reject a duplicate active invitation for the same (project, user).
    existing = ProjectInvitation.objects.filter(
        project=project,
        invited_user=invited,
        status=InvitationStatus.PENDING,
    ).first()
    if existing is not None:
        if existing.is_expired():
            _expire_stale(existing)
        else:
            raise errors.InvitationAlreadyExists()

    raw_token = generate_invitation_token()
    with transaction.atomic():
        try:
            invitation = ProjectInvitation.objects.create(
                project=project,
                invited_by=actor,
                invited_user=invited,
                token_hash=hash_invitation_token(raw_token),
                access_role=access_role,
                team_role=team_role,
                custom_team_role=custom_team_role,
                invitation_type=InvitationType.USERNAME,
                status=InvitationStatus.PENDING,
                expires_at=ProjectInvitation.default_expiry(),
            )
        except IntegrityError as exc:
            # Lost the race against the partial-unique constraint.
            raise errors.InvitationAlreadyExists() from exc
        record_activity(
            project,
            actor,
            "member_invited",
            title=invited.username,
            description="приглашение по username",
            metadata={"access_role": access_role, "invitation_type": "username"},
            target_type="invitation",
            target_id=str(invitation.id),
        )
    return invitation, raw_token


def create_link_invitation(
    actor: User,
    project: Project,
    *,
    access_role: str,
    team_role: str = "",
    custom_team_role: str = "",
) -> tuple[ProjectInvitation, str]:
    """Create a one-time shareable link invitation. Returns (invitation, raw_token)."""
    _require_manage(actor, project)
    access_role = _validate_access_role(access_role)
    team_role, custom_team_role = _normalize_team_role(team_role, custom_team_role)

    raw_token = generate_invitation_token()
    with transaction.atomic():
        invitation = ProjectInvitation.objects.create(
            project=project,
            invited_by=actor,
            invited_user=None,
            token_hash=hash_invitation_token(raw_token),
            access_role=access_role,
            team_role=team_role,
            custom_team_role=custom_team_role,
            invitation_type=InvitationType.LINK,
            status=InvitationStatus.PENDING,
            expires_at=ProjectInvitation.default_expiry(),
        )
        record_activity(
            project,
            actor,
            "member_invited",
            title="Ссылка-приглашение",
            description="приглашение по ссылке",
            metadata={"access_role": access_role, "invitation_type": "link"},
            target_type="invitation",
            target_id=str(invitation.id),
        )
    return invitation, raw_token


def list_pending_invitations(project: Project):
    """Project-side pending invitations (lazily expiring stale ones)."""
    qs = (
        ProjectInvitation.objects.filter(
            project=project, status=InvitationStatus.PENDING
        )
        .select_related("invited_by", "invited_user")
        .order_by("-created_at")
    )
    fresh = []
    for inv in qs:
        if inv.is_expired():
            _expire_stale(inv)
            continue
        fresh.append(inv)
    return fresh


def list_incoming_invitations(user: User):
    """Username invitations addressed to ``user`` that are still actionable."""
    qs = (
        ProjectInvitation.objects.filter(
            invited_user=user, status=InvitationStatus.PENDING
        )
        .select_related("project", "invited_by")
        .order_by("-created_at")
    )
    fresh = []
    for inv in qs:
        if inv.is_expired():
            _expire_stale(inv)
            continue
        fresh.append(inv)
    return fresh


def _get_invitation_for_action(invitation: ProjectInvitation) -> ProjectInvitation:
    """Validate an invitation is still usable, raising the precise error."""
    if invitation.status == InvitationStatus.CANCELLED:
        raise errors.InvitationCancelled()
    if invitation.status == InvitationStatus.ACCEPTED:
        raise errors.InvitationAlreadyUsed()
    if invitation.status in (InvitationStatus.DECLINED, InvitationStatus.EXPIRED):
        raise errors.InvitationAlreadyUsed()
    if invitation.is_expired():
        _expire_stale(invitation)
        raise errors.InvitationExpired()
    return invitation


def accept_invitation(user: User, raw_token: str) -> ProjectMember:
    """Accept by raw token. Creates/activates the ProjectMember atomically."""
    invitation = _lookup_by_token(raw_token)
    if invitation is None:
        raise errors.InvitationNotFound()
    return accept_invitation_obj(user, invitation)


def accept_invitation_obj(user: User, invitation: ProjectInvitation) -> ProjectMember:
    with transaction.atomic():
        invitation = (
            ProjectInvitation.objects.select_for_update()
            .select_related("project")
            .get(pk=invitation.pk)
        )
        _get_invitation_for_action(invitation)

        # Username invitations may only be accepted by the named user.
        if (
            invitation.invitation_type == InvitationType.USERNAME
            and invitation.invited_user_id is not None
            and invitation.invited_user_id != user.id
        ):
            raise errors.WrongInvitedUser()

        project = invitation.project

        if policy.is_member(user, project):
            # Already a member — consume the invite but don't duplicate.
            invitation.status = InvitationStatus.ACCEPTED
            invitation.accepted_by = user
            invitation.accepted_at = timezone.now()
            invitation.save(
                update_fields=["status", "accepted_by", "accepted_at", "updated_at"]
            )
            raise errors.AlreadyMember()

        member, _created = ProjectMember.objects.get_or_create(
            project=project,
            user=user,
            defaults={
                "role": invitation.access_role,
                "team_role": invitation.team_role,
                "custom_team_role": invitation.custom_team_role,
                "joined_at": timezone.now(),
            },
        )

        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_by = user
        invitation.accepted_at = timezone.now()
        invitation.save(
            update_fields=["status", "accepted_by", "accepted_at", "updated_at"]
        )

        record_activity(
            project,
            user,
            "invitation_accepted",
            title=user.username,
            description="приглашение принято",
            metadata={"access_role": invitation.access_role},
            target_type="member",
            target_id=str(member.id),
        )
    return member


def decline_invitation_obj(user: User, invitation: ProjectInvitation) -> None:
    with transaction.atomic():
        invitation = ProjectInvitation.objects.select_for_update().get(pk=invitation.pk)
        _get_invitation_for_action(invitation)
        if (
            invitation.invited_user_id is not None
            and invitation.invited_user_id != user.id
        ):
            raise errors.WrongInvitedUser()
        invitation.status = InvitationStatus.DECLINED
        invitation.save(update_fields=["status", "updated_at"])
        record_activity(
            invitation.project,
            user,
            "invitation_declined",
            title=user.username,
            description="приглашение отклонено",
            target_type="invitation",
            target_id=str(invitation.id),
        )


def cancel_invitation(actor: User, project: Project, invitation_id: int) -> None:
    _require_manage(actor, project)
    invitation = ProjectInvitation.objects.filter(
        project=project, pk=invitation_id
    ).first()
    if invitation is None:
        raise errors.InvitationNotFound()
    if invitation.status != InvitationStatus.PENDING:
        # Already resolved — nothing to cancel.
        if invitation.status == InvitationStatus.ACCEPTED:
            raise errors.InvitationAlreadyUsed()
        raise errors.InvitationNotFound()
    invitation.status = InvitationStatus.CANCELLED
    invitation.save(update_fields=["status", "updated_at"])
    record_activity(
        project,
        actor,
        "invitation_cancelled",
        title=(invitation.invited_user.username if invitation.invited_user else "Ссылка"),
        description="приглашение отменено",
        target_type="invitation",
        target_id=str(invitation.id),
    )


# --------------------------------------------------------------------------- #
# Member management
# --------------------------------------------------------------------------- #

def change_member_access_role(
    actor: User, project: Project, member_id: int, new_role: str
) -> ProjectMember:
    actor_role = _require_manage(actor, project)
    new_role = _validate_access_role(new_role)
    member = get_member(project, member_id)

    if member.role == ProjectMemberRole.OWNER:
        # The owner's role is immutable through this path.
        raise errors.CannotAssignOwner()

    # Admins may only manage editors/viewers, not other admins (task §1: admin
    # "изменять роли редакторов и наблюдателей"). Owner may manage anyone.
    if actor_role == ProjectMemberRole.ADMIN and member.role == ProjectMemberRole.ADMIN:
        raise errors.InsufficientPermissions()

    if member.role == new_role:
        return member

    old_role = member.role
    member.role = new_role
    member.save(update_fields=["role", "updated_at"])
    record_activity(
        project,
        actor,
        "member_role_changed",
        title=member.user.username,
        description=f"{old_role} → {new_role}",
        metadata={"from": old_role, "to": new_role},
        target_type="member",
        target_id=str(member.id),
    )
    return member


def change_member_team_role(
    actor: User,
    project: Project,
    member_id: int,
    team_role: str,
    custom_team_role: str = "",
) -> ProjectMember:
    _require_manage(actor, project)
    team_role, custom_team_role = _normalize_team_role(team_role, custom_team_role)
    member = get_member(project, member_id)
    member.team_role = team_role
    member.custom_team_role = custom_team_role
    member.save(update_fields=["team_role", "custom_team_role", "updated_at"])
    return member


def remove_member(actor: User, project: Project, member_id: int) -> None:
    actor_role = _require_manage(actor, project)
    member = get_member(project, member_id)

    if member.role == ProjectMemberRole.OWNER:
        raise errors.CannotRemoveOwner()
    # Admin cannot remove another admin (only the owner can).
    if actor_role == ProjectMemberRole.ADMIN and member.role == ProjectMemberRole.ADMIN:
        raise errors.InsufficientPermissions()

    username = member.user.username
    member_pk = member.id
    member.delete()
    record_activity(
        project,
        actor,
        "member_removed",
        title=username,
        description="участник удалён",
        target_type="member",
        target_id=str(member_pk),
    )


def leave_project(actor: User, project: Project) -> None:
    role = policy.get_role(actor, project)
    if role is None:
        raise errors.MemberNotFound()
    if role == ProjectMemberRole.OWNER:
        raise errors.OwnerCannotLeave()

    member = ProjectMember.objects.filter(project=project, user=actor).first()
    if member is None:
        raise errors.MemberNotFound()
    member_pk = member.id
    member.delete()
    record_activity(
        project,
        actor,
        "member_left",
        title=actor.username,
        description="участник покинул проект",
        target_type="member",
        target_id=str(member_pk),
    )


def transfer_ownership(actor: User, project: Project, new_owner_member_id: int) -> None:
    """Atomically hand ownership to another active member.

    Old owner becomes admin; the target becomes owner; the project's owner FK is
    repointed. Exactly one owner remains.
    """
    if not policy.can_transfer_ownership(actor, project):
        raise errors.InsufficientPermissions()

    with transaction.atomic():
        target = (
            ProjectMember.objects.select_for_update()
            .filter(project=project, pk=new_owner_member_id)
            .select_related("user")
            .first()
        )
        if target is None:
            raise errors.MemberNotFound()
        if target.user_id == actor.id:
            # Transferring to self is a no-op that would break the single-owner
            # invariant logic below — reject clearly.
            raise errors.InvalidRole("Нельзя передать владение самому себе.")

        # Resolve / ensure the current owner's membership row.
        owner_member, _ = ProjectMember.objects.select_for_update().get_or_create(
            project=project,
            user=actor,
            defaults={"role": ProjectMemberRole.OWNER, "joined_at": timezone.now()},
        )

        owner_member.role = ProjectMemberRole.ADMIN
        owner_member.save(update_fields=["role", "updated_at"])

        target.role = ProjectMemberRole.OWNER
        target.save(update_fields=["role", "updated_at"])

        # Keep the Project.owner FK consistent (the policy treats owner_id as an
        # ownership signal). Legacy ``user`` (UserKey) is left untouched.
        project.owner_id = target.user_id
        project.save(update_fields=["owner"])

        record_activity(
            project,
            actor,
            "ownership_transferred",
            title=target.user.username,
            description="владение передано",
            metadata={"new_owner_user_id": target.user_id},
            target_type="member",
            target_id=str(target.id),
        )


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #

def _lookup_by_token(raw_token: str) -> Optional[ProjectInvitation]:
    if not raw_token:
        return None
    return (
        ProjectInvitation.objects.filter(token_hash=hash_invitation_token(raw_token))
        .select_related("project", "invited_user", "invited_by")
        .first()
    )


def get_invitation_by_token(raw_token: str) -> Optional[ProjectInvitation]:
    """Public token lookup (used by the accept-by-link preview endpoint)."""
    return _lookup_by_token(raw_token)


def _resolve_user_by_username(username: str) -> Optional[User]:
    """Resolve a user by their public username (profile) or auth username.

    WCraft users may not have an email, so we never key on email. Public
    username (UserProfile.public_username) is preferred; we fall back to the
    Django auth username.
    """
    try:
        from w_craft_back.profile.models import UserProfile

        profile = (
            UserProfile.objects.select_related("user")
            .filter(public_username__iexact=username)
            .first()
        )
        if profile is not None:
            return profile.user
    except Exception:  # pragma: no cover - profile app optional
        pass
    return User.objects.filter(username__iexact=username).first()
