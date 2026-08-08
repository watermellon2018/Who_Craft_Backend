"""Team-collaboration models: project invitations.

ProjectMember / ProjectActivity / the role enums live in ``dashboard_models``
(they pre-date this feature). This module adds the invitation entity used to
bring new users into a project's team, either by username or by a shareable
one-time link.

Security notes:
- The raw invitation token is NEVER stored. We keep only a SHA-256 hash
  (``token_hash``) and return the raw token once, at creation time, to the
  caller. Lookups hash the supplied token and compare.
- Invitations grant no access until explicitly accepted. Acceptance is what
  creates / activates the ProjectMember.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from w_craft_back.movie.project.dashboard_models import (
    ProjectMemberRole,
    ProjectTeamRole,
)
from w_craft_back.movie.project.models import Project


# Any invitation expires this many days after creation (task §4).
INVITATION_TTL_DAYS = 5


def hash_invitation_token(raw_token: str) -> str:
    """Return the stable SHA-256 hex digest used to look an invitation up."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_invitation_token() -> str:
    """Cryptographically strong, URL-safe, single-use link token."""
    return secrets.token_urlsafe(32)


class InvitationType(models.TextChoices):
    USERNAME = "username", "По username"
    LINK = "link", "По ссылке"


class InvitationStatus(models.TextChoices):
    PENDING = "pending", "Ожидает"
    ACCEPTED = "accepted", "Принято"
    DECLINED = "declined", "Отклонено"
    CANCELLED = "cancelled", "Отменено"
    EXPIRED = "expired", "Истекло"


class ProjectInvitation(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_project_invitations",
    )
    # For username invitations this is the targeted user. Null for link
    # invitations (anyone with the link who is logged in may accept).
    invited_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="received_project_invitations",
    )
    # SHA-256 hash of the raw token. The raw token is shown once at creation.
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)

    access_role = models.CharField(
        max_length=20,
        choices=ProjectMemberRole.choices,
        default=ProjectMemberRole.VIEWER,
    )
    team_role = models.CharField(
        max_length=32,
        choices=ProjectTeamRole.choices,
        blank=True,
        default="",
    )
    custom_team_role = models.CharField(max_length=64, blank=True, default="")

    invitation_type = models.CharField(
        max_length=16, choices=InvitationType.choices
    )
    status = models.CharField(
        max_length=16,
        choices=InvitationStatus.choices,
        default=InvitationStatus.PENDING,
        db_index=True,
    )
    expires_at = models.DateTimeField()

    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_project_invitations",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["invited_user", "status"]),
            models.Index(fields=["status", "expires_at"]),
        ]
        constraints = [
            # At most one PENDING username-invitation per (project, invited_user).
            # Link invitations (invited_user IS NULL) are exempt — each link is a
            # distinct one-time invite, so several may be pending at once.
            models.UniqueConstraint(
                fields=["project", "invited_user"],
                condition=models.Q(
                    status="pending", invited_user__isnull=False
                ),
                name="uniq_pending_username_invitation",
            ),
        ]

    def __str__(self):
        target = self.invited_user_id or "link"
        return f"Invitation[{self.project_id}->{target}] {self.status}"

    # -- lifecycle helpers --------------------------------------------------- #

    @classmethod
    def default_expiry(cls):
        return timezone.now() + timedelta(days=INVITATION_TTL_DAYS)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_actionable(self) -> bool:
        """True only when the invitation can still be accepted/declined."""
        return (
            self.status == InvitationStatus.PENDING and not self.is_expired()
        )

    def team_role_label(self) -> str:
        if self.team_role == ProjectTeamRole.OTHER:
            return self.custom_team_role or "Другое"
        if not self.team_role:
            return ""
        return ProjectTeamRole(self.team_role).label
