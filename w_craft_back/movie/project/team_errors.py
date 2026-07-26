"""Structured error codes for the team-collaboration API.

Every team operation raises a ``TeamError`` carrying a stable ``code`` (consumed
by the frontend for localized messaging) and an HTTP status. Views translate
these into JSON ``{"code": ..., "detail": ...}`` responses.
"""

from __future__ import annotations


class TeamError(Exception):
    code = "TEAM_ERROR"
    status = 400
    detail = "Team operation failed."

    def __init__(self, detail: str | None = None):
        if detail:
            self.detail = detail
        super().__init__(self.detail)

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


class UserNotFound(TeamError):
    code = "USER_NOT_FOUND"
    status = 404
    detail = "Пользователь не найден."


class AlreadyMember(TeamError):
    code = "ALREADY_MEMBER"
    status = 409
    detail = "Пользователь уже состоит в команде проекта."


class InvitationAlreadyExists(TeamError):
    code = "INVITATION_ALREADY_EXISTS"
    status = 409
    detail = "Для этого пользователя уже есть активное приглашение."


class InvitationExpired(TeamError):
    code = "INVITATION_EXPIRED"
    status = 410
    detail = "Срок действия приглашения истёк."


class InvitationCancelled(TeamError):
    code = "INVITATION_CANCELLED"
    status = 409
    detail = "Приглашение было отменено."


class InvitationAlreadyUsed(TeamError):
    code = "INVITATION_ALREADY_USED"
    status = 409
    detail = "Приглашение уже использовано."


class InvitationNotFound(TeamError):
    code = "INVITATION_NOT_FOUND"
    status = 404
    detail = "Приглашение не найдено."


class InsufficientPermissions(TeamError):
    code = "INSUFFICIENT_PERMISSIONS"
    status = 403
    detail = "Недостаточно прав для этого действия."


class OwnerCannotLeave(TeamError):
    code = "OWNER_CANNOT_LEAVE"
    status = 409
    detail = "Владелец не может покинуть проект, не передав владение."


class CannotRemoveOwner(TeamError):
    code = "CANNOT_REMOVE_OWNER"
    status = 409
    detail = "Нельзя удалить владельца проекта."


class CannotAssignOwner(TeamError):
    code = "CANNOT_ASSIGN_OWNER"
    status = 409
    detail = "Роль владельца назначается только через передачу владения."


class MemberNotFound(TeamError):
    code = "MEMBER_NOT_FOUND"
    status = 404
    detail = "Участник не найден."


class InvalidRole(TeamError):
    code = "INVALID_ROLE"
    status = 400
    detail = "Недопустимая роль."


class CannotInviteSelf(TeamError):
    code = "CANNOT_INVITE_SELF"
    status = 400
    detail = "Нельзя пригласить самого себя."


class WrongInvitedUser(TeamError):
    code = "WRONG_INVITED_USER"
    status = 403
    detail = "Это приглашение предназначено другому пользователю."
