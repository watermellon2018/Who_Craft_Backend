"""Tests for project team collaboration: roles, members, invitations,
ownership transfer, author preservation, and version conflicts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project import policy, team_service
from w_craft_back.movie.project import team_errors as errors
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
    Scene,
)
from w_craft_back.movie.project.models import Project, ProjectStatus
from w_craft_back.movie.project.team_models import (
    InvitationStatus,
    InvitationType,
    ProjectInvitation,
)
from w_craft_back.notifications.models import Notification
from w_craft_back.profile.models import UserProfile


def _make_user(username: str) -> tuple[User, str]:
    user = User.objects.create_user(username=username, password="pw")
    key = UserKey.objects.create(user=user)
    return user, str(key.key)


def _make_project(owner: User, *, title: str = "Demo") -> Project:
    project = Project.objects.create(
        owner=owner,
        title=title,
        format="feature_film",
        annotation="",
        synopsis="legacy",
        summary="desc",
        status=ProjectStatus.IN_PROGRESS,
    )
    ProjectMember.objects.create(
        project=project, user=owner, role=ProjectMemberRole.OWNER,
        joined_at=timezone.now(),
    )
    return project


def _add_member(project, user, role) -> ProjectMember:
    return ProjectMember.objects.create(
        project=project, user=user, role=role, joined_at=timezone.now()
    )


# --------------------------------------------------------------------------- #
# Roles / single-owner invariant / membership
# --------------------------------------------------------------------------- #

class MembershipInvariantTests(TestCase):
    def setUp(self):
        self.owner, _ = _make_user("owner")
        self.project = _make_project(self.owner)

    def test_project_has_single_owner(self):
        owners = ProjectMember.objects.filter(
            project=self.project, role=ProjectMemberRole.OWNER
        )
        self.assertEqual(owners.count(), 1)

    def test_cannot_create_duplicate_member(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProjectMember.objects.create(
                    project=self.project, user=self.owner,
                    role=ProjectMemberRole.EDITOR,
                )

    def test_cannot_create_second_owner_member(self):
        other, _ = _make_user("second-owner")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProjectMember.objects.create(
                    project=self.project,
                    user=other,
                    role=ProjectMemberRole.OWNER,
                )

    def test_deleting_userkey_preserves_project(self):
        UserKey.objects.get(user=self.owner).delete()

        self.project.refresh_from_db()
        self.assertEqual(self.project.owner_id, self.owner.id)

    def test_deleting_current_owner_account_is_protected(self):
        with self.assertRaises(ProtectedError):
            self.owner.delete()

        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())


class PolicyMatrixTests(TestCase):
    def setUp(self):
        from w_craft_back.movie.project import policy

        self.policy = policy
        self.owner, _ = _make_user("owner")
        self.admin, _ = _make_user("admin")
        self.editor, _ = _make_user("editor")
        self.viewer, _ = _make_user("viewer")
        self.outsider, _ = _make_user("outsider")
        self.project = _make_project(self.owner)
        _add_member(self.project, self.admin, ProjectMemberRole.ADMIN)
        _add_member(self.project, self.editor, ProjectMemberRole.EDITOR)
        _add_member(self.project, self.viewer, ProjectMemberRole.VIEWER)

    def test_owner_full_access(self):
        p = self.policy
        self.assertTrue(p.can_edit(self.owner, self.project))
        self.assertTrue(p.can_manage_team(self.owner, self.project))
        self.assertTrue(p.can_delete_project(self.owner, self.project))
        self.assertTrue(p.can_transfer_ownership(self.owner, self.project))
        self.assertTrue(p.can_publish(self.owner, self.project))
        self.assertFalse(p.can_leave_project(self.owner, self.project))

    def test_admin_cannot_delete_or_transfer(self):
        p = self.policy
        self.assertTrue(p.can_manage_team(self.admin, self.project))
        self.assertTrue(p.can_edit(self.admin, self.project))
        self.assertTrue(p.can_publish(self.admin, self.project))
        self.assertFalse(p.can_delete_project(self.admin, self.project))
        self.assertFalse(p.can_transfer_ownership(self.admin, self.project))

    def test_editor_cannot_manage_team(self):
        p = self.policy
        self.assertTrue(p.can_edit(self.editor, self.project))
        self.assertTrue(p.can_run_generation(self.editor, self.project))
        self.assertFalse(p.can_manage_team(self.editor, self.project))
        self.assertFalse(p.can_publish(self.editor, self.project))
        self.assertFalse(p.can_delete_project(self.editor, self.project))

    def test_viewer_read_only(self):
        p = self.policy
        self.assertTrue(p.can_view(self.viewer, self.project))
        self.assertFalse(p.can_edit(self.viewer, self.project))
        self.assertFalse(p.can_run_generation(self.viewer, self.project))
        self.assertFalse(p.can_manage_team(self.viewer, self.project))

    def test_outsider_no_access(self):
        p = self.policy
        self.assertFalse(p.can_view(self.outsider, self.project))
        self.assertIsNone(p.get_role(self.outsider, self.project))

# --------------------------------------------------------------------------- #
# Username invitations
# --------------------------------------------------------------------------- #

class UsernameInvitationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_token = _make_user("owner")
        self.invitee, self.invitee_token = _make_user("invitee")
        self.editor, self.editor_token = _make_user("editor")
        self.project = _make_project(self.owner)
        _add_member(self.project, self.editor, ProjectMemberRole.EDITOR)

    def _invite_url(self):
        return f"/api/projects/{self.project.id}/team/invitations/"

    def test_invite_by_username(self):
        resp = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["invitationType"], "username")
        self.assertEqual(body["invitedUsername"], "invitee")
        inv = ProjectInvitation.objects.get(pk=body["id"])
        self.assertEqual(inv.status, InvitationStatus.PENDING)
        # No membership yet — invitation grants no access.
        self.assertFalse(
            ProjectMember.objects.filter(project=self.project, user=self.invitee).exists()
        )

    @patch('w_craft_back.notifications.services.send_notification_email')
    def test_username_invitation_dispatches_in_app_and_email_after_commit(self, send_email):
        self.invitee.email = 'invitee@example.test'
        self.invitee.save(update_fields=['email'])
        UserProfile.objects.create(
            user=self.invitee,
            notifications_in_app=True,
            notifications_email=True,
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self._invite_url(),
                data={'username': 'invitee', 'access_role': 'editor'},
                format='json',
                HTTP_X_USER_TOKEN=self.owner_token,
            )
        self.assertEqual(response.status_code, 201)
        notification = Notification.objects.get(recipient=self.invitee)
        self.assertEqual(notification.type, Notification.Type.PROJECT_INVITATION)
        self.assertEqual(notification.target_url, '/project-list')
        send_email.assert_called_once()

    def test_invite_unknown_username(self):
        resp = self.client.post(
            self._invite_url(),
            data={"username": "ghost", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "USER_NOT_FOUND")

    def test_cannot_invite_existing_member(self):
        resp = self.client.post(
            self._invite_url(),
            data={"username": "editor", "access_role": "viewer"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "ALREADY_MEMBER")

    def test_duplicate_pending_invitation_rejected(self):
        self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        resp = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "viewer"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "INVITATION_ALREADY_EXISTS")

    def test_editor_cannot_invite(self):
        resp = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "viewer"},
            format="json",
            HTTP_X_USER_TOKEN=self.editor_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "INSUFFICIENT_PERMISSIONS")

    def test_stale_inactive_owner_cannot_create_invitation(self):
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.client.force_authenticate(user=self.owner)

        resp = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "viewer"},
            format="json",
        )

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            ProjectInvitation.objects.filter(
                project=self.project,
                invited_user=self.invitee,
            ).exists()
        )

    def test_accept_creates_membership(self):
        invite = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        ).json()
        resp = self.client.post(
            f"/api/invitations/{invite['id']}/accept/",
            HTTP_X_USER_TOKEN=self.invitee_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        member = ProjectMember.objects.get(project=self.project, user=self.invitee)
        self.assertEqual(member.role, ProjectMemberRole.EDITOR)
        inv = ProjectInvitation.objects.get(pk=invite["id"])
        self.assertEqual(inv.status, InvitationStatus.ACCEPTED)

    def test_stale_inactive_invitee_cannot_accept_invitation(self):
        invite = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        ).json()
        self.invitee.is_active = False
        self.invitee.save(update_fields=["is_active"])
        self.client.force_authenticate(user=self.invitee)

        resp = self.client.post(f"/api/invitations/{invite['id']}/accept/")

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            ProjectMember.objects.filter(
                project=self.project,
                user=self.invitee,
            ).exists()
        )

    def test_decline_does_not_create_membership(self):
        invite = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        ).json()
        resp = self.client.post(
            f"/api/invitations/{invite['id']}/decline/",
            HTTP_X_USER_TOKEN=self.invitee_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            ProjectMember.objects.filter(project=self.project, user=self.invitee).exists()
        )
        inv = ProjectInvitation.objects.get(pk=invite["id"])
        self.assertEqual(inv.status, InvitationStatus.DECLINED)

    def test_username_invitation_wrong_user_rejected(self):
        invite = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        ).json()
        # A different user (editor) tries to accept invitee's invitation.
        resp = self.client.post(
            f"/api/invitations/{invite['id']}/accept/",
            HTTP_X_USER_TOKEN=self.editor_token,
        )
        # editor is already a member, but the wrong-user check comes first.
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "WRONG_INVITED_USER")

    def test_expired_invitation_cannot_be_accepted(self):
        invite = ProjectInvitation.objects.create(
            project=self.project,
            invited_by=self.owner,
            invited_user=self.invitee,
            token_hash="x" * 64,
            access_role=ProjectMemberRole.EDITOR,
            invitation_type=InvitationType.USERNAME,
            status=InvitationStatus.PENDING,
            expires_at=timezone.now() - timedelta(days=1),
        )
        resp = self.client.post(
            f"/api/invitations/{invite.id}/accept/",
            HTTP_X_USER_TOKEN=self.invitee_token,
        )
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.json()["code"], "INVITATION_EXPIRED")

    def test_cancelled_invitation_cannot_be_accepted(self):
        invite = ProjectInvitation.objects.create(
            project=self.project,
            invited_by=self.owner,
            invited_user=self.invitee,
            token_hash="y" * 64,
            access_role=ProjectMemberRole.EDITOR,
            invitation_type=InvitationType.USERNAME,
            status=InvitationStatus.CANCELLED,
            expires_at=timezone.now() + timedelta(days=2),
        )
        resp = self.client.post(
            f"/api/invitations/{invite.id}/accept/",
            HTTP_X_USER_TOKEN=self.invitee_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "INVITATION_CANCELLED")

    def test_invitation_cannot_be_reused(self):
        invite = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        ).json()
        self.client.post(
            f"/api/invitations/{invite['id']}/accept/",
            HTTP_X_USER_TOKEN=self.invitee_token,
        )
        # Second accept fails.
        resp = self.client.post(
            f"/api/invitations/{invite['id']}/accept/",
            HTTP_X_USER_TOKEN=self.invitee_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "INVITATION_ALREADY_USED")

    def test_default_expiry_is_five_days(self):
        invite = self.client.post(
            self._invite_url(),
            data={"username": "invitee", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        ).json()
        inv = ProjectInvitation.objects.get(pk=invite["id"])
        delta = inv.expires_at - inv.created_at
        # Allow a small margin for execution time.
        self.assertAlmostEqual(delta.total_seconds(), timedelta(days=5).total_seconds(), delta=120)


# --------------------------------------------------------------------------- #
# Link invitations
# --------------------------------------------------------------------------- #

class LinkInvitationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_token = _make_user("owner")
        self.joiner, self.joiner_token = _make_user("joiner")
        self.other, self.other_token = _make_user("other")
        self.project = _make_project(self.owner)

    def _invite_url(self):
        return f"/api/projects/{self.project.id}/team/invitations/"

    @override_settings(FRONTEND_BASE_URL="http://frontend.test:3000")
    def test_create_link_invitation_returns_token_once(self):
        resp = self.client.post(
            self._invite_url(),
            data={"invitation_type": "link", "access_role": "viewer"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertIn("token", body)
        self.assertIn("inviteUrl", body)
        self.token = body["token"]
        self.assertEqual(
            body["inviteUrl"],
            f"http://frontend.test:3000/invite/{self.token}",
        )
        # Token is NOT stored raw.
        inv = ProjectInvitation.objects.get(pk=body["id"])
        self.assertNotEqual(inv.token_hash, self.token)
        # Listing pending invites must not leak the token.
        listing = self.client.get(
            self._invite_url(), HTTP_X_USER_TOKEN=self.owner_token
        ).json()
        self.assertNotIn("token", listing["invitations"][0])

    def test_link_invitation_is_one_time(self):
        token = self.client.post(
            self._invite_url(),
            data={"invitation_type": "link", "access_role": "viewer"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        ).json()["token"]
        # First accept succeeds.
        resp1 = self.client.post(
            f"/api/invitations/token/{token}/", HTTP_X_USER_TOKEN=self.joiner_token
        )
        self.assertEqual(resp1.status_code, 200, resp1.content)
        self.assertTrue(
            ProjectMember.objects.filter(project=self.project, user=self.joiner).exists()
        )
        # Second accept (different user) fails — already used.
        resp2 = self.client.post(
            f"/api/invitations/token/{token}/", HTTP_X_USER_TOKEN=self.other_token
        )
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.json()["code"], "INVITATION_ALREADY_USED")

    def test_pending_invitation_persists_across_reload(self):
        self.client.post(
            self._invite_url(),
            data={"invitation_type": "link", "access_role": "viewer"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        listing = self.client.get(
            self._invite_url(), HTTP_X_USER_TOKEN=self.owner_token
        ).json()
        self.assertEqual(len(listing["invitations"]), 1)


# --------------------------------------------------------------------------- #
# Member removal / leave / ownership transfer
# --------------------------------------------------------------------------- #

class MemberManagementTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_token = _make_user("owner")
        self.admin, self.admin_token = _make_user("admin")
        self.editor, self.editor_token = _make_user("editor")
        self.viewer, self.viewer_token = _make_user("viewer")
        self.project = _make_project(self.owner)
        self.admin_m = _add_member(self.project, self.admin, ProjectMemberRole.ADMIN)
        self.editor_m = _add_member(self.project, self.editor, ProjectMemberRole.EDITOR)
        self.viewer_m = _add_member(self.project, self.viewer, ProjectMemberRole.VIEWER)
        self.owner_m = ProjectMember.objects.get(
            project=self.project, user=self.owner
        )

    def _member_url(self, member_id):
        return f"/api/projects/{self.project.id}/team/members/{member_id}/"

    def test_remove_member_revokes_access(self):
        resp = self.client.delete(
            self._member_url(self.editor_m.id), HTTP_X_USER_TOKEN=self.owner_token
        )
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(
            ProjectMember.objects.filter(pk=self.editor_m.id).exists()
        )
        # Editor now gets 403 on the dashboard.
        dash = self.client.get(
            f"/api/projects/{self.project.id}/dashboard/",
            HTTP_X_USER_TOKEN=self.editor_token,
        )
        self.assertEqual(dash.status_code, 403)

    def test_admin_cannot_remove_owner(self):
        resp = self.client.delete(
            self._member_url(self.owner_m.id), HTTP_X_USER_TOKEN=self.admin_token
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "CANNOT_REMOVE_OWNER")

    def test_editor_cannot_remove_member(self):
        resp = self.client.delete(
            self._member_url(self.viewer_m.id), HTTP_X_USER_TOKEN=self.editor_token
        )
        self.assertEqual(resp.status_code, 403)

    def test_leave_project_revokes_access(self):
        resp = self.client.post(
            f"/api/projects/{self.project.id}/team/leave/",
            HTTP_X_USER_TOKEN=self.editor_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            ProjectMember.objects.filter(project=self.project, user=self.editor).exists()
        )

    def test_owner_cannot_leave(self):
        resp = self.client.post(
            f"/api/projects/{self.project.id}/team/leave/",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "OWNER_CANNOT_LEAVE")

    def test_transfer_ownership_keeps_single_owner(self):
        resp = self.client.post(
            f"/api/projects/{self.project.id}/team/transfer-ownership/",
            data={"member_id": self.admin_m.id},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        owners = ProjectMember.objects.filter(
            project=self.project, role=ProjectMemberRole.OWNER
        )
        self.assertEqual(owners.count(), 1)
        self.assertEqual(owners.first().user_id, self.admin.id)
        # Former owner is now admin.
        self.owner_m.refresh_from_db()
        self.assertEqual(self.owner_m.role, ProjectMemberRole.ADMIN)
        # Project.owner FK repointed.
        self.project.refresh_from_db()
        self.assertEqual(self.project.owner_id, self.admin.id)
        self.assertEqual(
            policy.get_role(self.owner, self.project), ProjectMemberRole.ADMIN,
        )
        self.assertFalse(policy.can_transfer_ownership(self.owner, self.project))

    def test_transfer_rejects_inactive_target_under_user_lock(self):
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])

        with self.assertRaises(errors.UserNotFound):
            team_service.transfer_ownership(
                self.owner,
                self.project,
                self.admin_m.id,
            )

        self.project.refresh_from_db()
        self.assertEqual(self.project.owner_id, self.owner.id)

    def test_stale_former_owner_cannot_transfer_again(self):
        team_service.transfer_ownership(
            self.owner, self.project, self.admin_m.id,
        )

        with self.assertRaises(errors.InsufficientPermissions):
            team_service.transfer_ownership(
                self.owner, self.project, self.editor_m.id,
            )

        self.project.refresh_from_db()
        self.assertEqual(self.project.owner_id, self.admin.id)
        self.assertEqual(
            ProjectMember.objects.get(project=self.project, user=self.admin).role,
            ProjectMemberRole.OWNER,
        )

    def test_deleting_former_owner_account_preserves_transferred_project(self):
        team_service.transfer_ownership(
            self.owner, self.project, self.admin_m.id,
        )
        self.owner.delete()

        self.project.refresh_from_db()
        self.assertEqual(self.project.owner_id, self.admin.id)
        self.assertEqual(
            ProjectMember.objects.get(project=self.project, user=self.admin).role,
            ProjectMemberRole.OWNER,
        )

    def test_delete_endpoint_follows_canonical_owner_after_transfer(self):
        team_service.transfer_ownership(
            self.owner,
            self.project,
            self.admin_m.id,
        )

        dashboard_delete = self.client.delete(
            f"/api/projects/{self.project.id}/",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(dashboard_delete.status_code, 403)

        new_owner_delete = self.client.delete(
            f"/api/projects/{self.project.id}/",
            HTTP_X_USER_TOKEN=self.admin_token,
        )
        self.assertEqual(new_owner_delete.status_code, 204)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())

    def test_admin_cannot_transfer_ownership(self):
        resp = self.client.post(
            f"/api/projects/{self.project.id}/team/transfer-ownership/",
            data={"member_id": self.editor_m.id},
            format="json",
            HTTP_X_USER_TOKEN=self.admin_token,
        )
        self.assertEqual(resp.status_code, 403)

    def test_cannot_assign_owner_via_role_change(self):
        resp = self.client.patch(
            self._member_url(self.editor_m.id),
            data={"access_role": "owner"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        # owner is not in the assignable choices → validation error or
        # CANNOT_ASSIGN_OWNER. Either way, not 200.
        self.assertIn(resp.status_code, (400, 409))

    def test_change_team_role(self):
        resp = self.client.patch(
            self._member_url(self.editor_m.id),
            data={"team_role": "screenwriter"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.editor_m.refresh_from_db()
        self.assertEqual(self.editor_m.team_role, "screenwriter")

    def test_custom_team_role_requires_label(self):
        resp = self.client.patch(
            self._member_url(self.editor_m.id),
            data={"team_role": "other", "custom_team_role": ""},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 400)


# --------------------------------------------------------------------------- #
# Concurrent ownership transfer
# --------------------------------------------------------------------------- #

class ConcurrentOwnershipTransferTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.owner, _ = _make_user("concurrent-owner")
        self.admin, _ = _make_user("concurrent-admin")
        self.editor, _ = _make_user("concurrent-editor")
        self.project = _make_project(self.owner)
        self.admin_member = _add_member(
            self.project, self.admin, ProjectMemberRole.ADMIN,
        )
        self.editor_member = _add_member(
            self.project, self.editor, ProjectMemberRole.EDITOR,
        )

    def test_only_one_concurrent_transfer_succeeds(self):
        barrier = Barrier(2)

        def attempt_transfer(member_id):
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.owner.pk)
                stale_project = Project.objects.get(pk=self.project.pk)
                barrier.wait(timeout=10)
                try:
                    team_service.transfer_ownership(
                        actor, stale_project, member_id,
                    )
                except errors.InsufficientPermissions:
                    return "denied"
                return "transferred"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(attempt_transfer, self.admin_member.pk),
                executor.submit(attempt_transfer, self.editor_member.pk),
            ]
            results = [future.result(timeout=20) for future in futures]

        self.assertCountEqual(results, ["transferred", "denied"])
        self.project.refresh_from_db()
        owners = ProjectMember.objects.filter(
            project=self.project,
            role=ProjectMemberRole.OWNER,
        )
        self.assertEqual(owners.count(), 1)
        self.assertEqual(owners.get().user_id, self.project.owner_id)
        self.assertIn(self.project.owner_id, (self.admin.id, self.editor.id))

    def _paused_transfer(self, member_id, entered, release):
        close_old_connections()
        try:
            actor = User.objects.get(pk=self.owner.pk)
            stale_project = Project.objects.get(pk=self.project.pk)

            def hold_before_commit(*args, **kwargs):
                entered.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("Timed out waiting to finish transfer")

            with patch.object(
                team_service,
                "record_activity",
                side_effect=hold_before_commit,
            ):
                team_service.transfer_ownership(
                    actor,
                    stale_project,
                    member_id,
                )
            return "transferred"
        finally:
            close_old_connections()

    def test_concurrent_former_owner_delete_is_denied_after_transfer(self):
        transfer_paused = Event()
        release_transfer = Event()
        delete_started = Event()

        def attempt_delete():
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.owner.pk)
                delete_started.set()
                try:
                    team_service.delete_project(actor, self.project.pk)
                except errors.InsufficientPermissions:
                    return "denied"
                return "deleted"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            transfer_future = executor.submit(
                self._paused_transfer,
                self.admin_member.pk,
                transfer_paused,
                release_transfer,
            )
            self.assertTrue(transfer_paused.wait(timeout=10))
            delete_future = executor.submit(attempt_delete)
            self.assertTrue(delete_started.wait(timeout=10))
            release_transfer.set()

            self.assertEqual(transfer_future.result(timeout=20), "transferred")
            self.assertEqual(delete_future.result(timeout=20), "denied")

        self.project.refresh_from_db()
        self.assertEqual(self.project.owner_id, self.admin.id)

    def test_concurrent_target_removal_cannot_drop_new_owner_member(self):
        transfer_paused = Event()
        release_transfer = Event()
        removal_started = Event()

        def attempt_removal():
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.owner.pk)
                stale_project = Project.objects.get(pk=self.project.pk)
                removal_started.set()
                try:
                    team_service.remove_member(
                        actor,
                        stale_project,
                        self.admin_member.pk,
                    )
                except (
                    errors.CannotRemoveOwner,
                    errors.InsufficientPermissions,
                ):
                    return "protected"
                return "removed"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            transfer_future = executor.submit(
                self._paused_transfer,
                self.admin_member.pk,
                transfer_paused,
                release_transfer,
            )
            self.assertTrue(transfer_paused.wait(timeout=10))
            removal_future = executor.submit(attempt_removal)
            self.assertTrue(removal_started.wait(timeout=10))
            release_transfer.set()

            self.assertEqual(transfer_future.result(timeout=20), "transferred")
            self.assertEqual(removal_future.result(timeout=20), "protected")

        self.project.refresh_from_db()
        owner_member = ProjectMember.objects.get(
            project=self.project,
            user_id=self.project.owner_id,
        )
        self.assertEqual(owner_member.role, ProjectMemberRole.OWNER)


# --------------------------------------------------------------------------- #
# Author preservation on member removal
# --------------------------------------------------------------------------- #

class AuthorPreservationTests(TestCase):
    def setUp(self):
        self.owner, _ = _make_user("owner")
        self.editor, _ = _make_user("editor")
        self.project = _make_project(self.owner)
        _add_member(self.project, self.editor, ProjectMemberRole.EDITOR)

    def test_material_survives_member_removal(self):
        scene = Scene.objects.create(
            project=self.project, title="By editor", order=1,
            created_by=self.editor, updated_by=self.editor,
        )
        # Remove the editor's membership.
        ProjectMember.objects.filter(project=self.project, user=self.editor).delete()
        scene.refresh_from_db()
        self.assertTrue(Scene.objects.filter(pk=scene.pk).exists())
        # created_by stays until the User itself is deleted.
        self.assertEqual(scene.created_by_id, self.editor.id)

    def test_author_set_null_on_user_delete(self):
        scene = Scene.objects.create(
            project=self.project, title="By editor", order=1,
            created_by=self.editor, updated_by=self.editor,
        )
        self.editor.delete()
        scene.refresh_from_db()
        self.assertIsNone(scene.created_by_id)
        self.assertTrue(Scene.objects.filter(pk=scene.pk).exists())


# --------------------------------------------------------------------------- #
# IDOR / nested-entity access
# --------------------------------------------------------------------------- #

class NestedEntityAccessTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_token = _make_user("owner")
        self.outsider, self.outsider_token = _make_user("outsider")
        self.member, self.member_token = _make_user("member")
        self.project = _make_project(self.owner)
        _add_member(self.project, self.member, ProjectMemberRole.EDITOR)
        self.scene = Scene.objects.create(
            project=self.project, title="S1", order=1, version=1,
            created_by=self.owner, updated_by=self.owner,
        )

    def _scene_url(self):
        return f"/api/projects/{self.project.id}/scenes/{self.scene.id}/"

    def test_outsider_cannot_get_scene(self):
        resp = self.client.get(self._scene_url(), HTTP_X_USER_TOKEN=self.outsider_token)
        self.assertEqual(resp.status_code, 403)

    def test_member_can_get_scene(self):
        resp = self.client.get(self._scene_url(), HTTP_X_USER_TOKEN=self.member_token)
        self.assertEqual(resp.status_code, 200)

    def test_no_token_returns_401(self):
        resp = self.client.get(self._scene_url())
        self.assertEqual(resp.status_code, 401)

    def test_scene_id_from_other_project_404(self):
        other_owner, other_token = _make_user("other")
        other_project = _make_project(other_owner, title="Other")
        # Try to read our scene id scoped under the other project → 404.
        url = f"/api/projects/{other_project.id}/scenes/{self.scene.id}/"
        resp = self.client.get(url, HTTP_X_USER_TOKEN=other_token)
        self.assertEqual(resp.status_code, 404)


# --------------------------------------------------------------------------- #
# Version conflict (409)
# --------------------------------------------------------------------------- #

class VersionConflictTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_token = _make_user("owner")
        self.project = _make_project(self.owner)
        self.scene = Scene.objects.create(
            project=self.project, title="S1", order=1, version=1,
            script_text="original", created_by=self.owner, updated_by=self.owner,
        )

    def _url(self):
        return f"/api/projects/{self.project.id}/scenes/{self.scene.id}/"

    def test_patch_with_current_version_succeeds_and_bumps(self):
        resp = self.client.patch(
            self._url(),
            data={"script_text": "v2", "version": 1},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["version"], 2)
        self.scene.refresh_from_db()
        self.assertEqual(self.scene.script_text, "v2")

    def test_patch_with_stale_version_returns_409(self):
        # Someone else bumped it to 2.
        Scene.objects.filter(pk=self.scene.pk).update(version=2, script_text="theirs")
        resp = self.client.patch(
            self._url(),
            data={"script_text": "mine", "version": 1},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        self.scene.refresh_from_db()
        # Not overwritten.
        self.assertEqual(self.scene.script_text, "theirs")


# --------------------------------------------------------------------------- #
# My Projects list includes team projects
# --------------------------------------------------------------------------- #

class ProjectListTeamTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_token = _make_user("owner")
        self.member, self.member_token = _make_user("member")
        self.own_project = _make_project(self.owner, title="Mine")
        self.team_project = _make_project(self.owner, title="Shared")
        _add_member(self.team_project, self.member, ProjectMemberRole.EDITOR)

    def test_member_sees_team_project_in_list(self):
        resp = self.client.get("/api/projects/", HTTP_X_USER_TOKEN=self.member_token)
        self.assertEqual(resp.status_code, 200)
        titles = {p["title"] for p in resp.json()["projects"]}
        self.assertIn("Shared", titles)
        self.assertNotIn("Mine", titles)

    def test_list_carries_role_and_team_flag(self):
        resp = self.client.get("/api/projects/", HTTP_X_USER_TOKEN=self.member_token)
        proj = next(p for p in resp.json()["projects"] if p["title"] == "Shared")
        self.assertEqual(proj["currentUserRole"], "editor")
        self.assertTrue(proj["isTeamProject"])
        self.assertGreaterEqual(proj["memberCount"], 2)

    def test_owner_sees_own_project_not_team_flagged(self):
        resp = self.client.get("/api/projects/", HTTP_X_USER_TOKEN=self.owner_token)
        proj = next(p for p in resp.json()["projects"] if p["title"] == "Mine")
        self.assertEqual(proj["currentUserRole"], "owner")
        self.assertFalse(proj["isTeamProject"])
        self.assertEqual(proj["memberCount"], 1)


# --------------------------------------------------------------------------- #
# Incoming invitations on "My Projects"
# --------------------------------------------------------------------------- #

class IncomingInvitationListTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_token = _make_user("owner")
        self.invitee, self.invitee_token = _make_user("invitee")
        self.project = _make_project(self.owner)

    def test_incoming_lists_pending_username_invite(self):
        self.client.post(
            f"/api/projects/{self.project.id}/team/invitations/",
            data={"username": "invitee", "access_role": "editor"},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        resp = self.client.get(
            "/api/invitations/incoming/", HTTP_X_USER_TOKEN=self.invitee_token
        )
        self.assertEqual(resp.status_code, 200)
        invites = resp.json()["invitations"]
        self.assertEqual(len(invites), 1)
        self.assertEqual(invites[0]["projectTitle"], "Demo")
        self.assertNotIn("token", invites[0])
