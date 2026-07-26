import uuid

from w_craft_back.auth.models import UserKey
from w_craft_back.auth.utils import extract_user_token
from w_craft_back.character_studio.services.errors import (
    PermissionDeniedError,
    ValidationError,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.policy import Action


def get_user_from_request(request):
    """Resolve calling ``UserKey``.

    Preference: ``X-User-Token`` header → request body. Query-string ``?token_user=``
    is intentionally NOT consulted (leaks into logs/history/referer). Falls back
    to ``request.user`` for session-authenticated callers.
    """
    token = extract_user_token(request)
    if not token and request.user and request.user.is_authenticated:
        try:
            return UserKey.objects.get(user=request.user)
        except UserKey.DoesNotExist as exc:
            raise PermissionDeniedError("Invalid user token.") from exc
    if not token:
        raise ValidationError(
            "Authentication token is required (send X-User-Token header)."
        )
    try:
        uuid.UUID(str(token))
    except (ValueError, TypeError) as exc:
        raise PermissionDeniedError("Invalid user token.") from exc
    try:
        return UserKey.objects.select_related("user").get(key=token)
    except UserKey.DoesNotExist as exc:
        raise PermissionDeniedError("Invalid user token.") from exc


def get_project_for_action(user, project_id, action: Action):
    """Return a project only when ``user`` may perform ``action``."""
    from w_craft_back.movie.project import policy

    try:
        project = Project.objects.select_related("owner", "user").get(id=project_id)
    except Project.DoesNotExist as exc:
        raise PermissionDeniedError() from exc
    if not policy.can(_auth_user(user), project, action):
        raise PermissionDeniedError()
    return project


def get_viewable_project(user, project_id):
    return get_project_for_action(user, project_id, Action.VIEW)


def get_editable_project(user, project_id):
    return get_project_for_action(user, project_id, Action.EDIT_CONTENT)


def get_generation_project(user, project_id):
    return get_project_for_action(user, project_id, Action.RUN_GENERATION)


def get_settings_project(user, project_id):
    return get_project_for_action(user, project_id, Action.EDIT_SETTINGS)


def _auth_user(user):
    """Resolve the Django User behind a principal that may be a UserKey."""
    if isinstance(user, UserKey):
        return user.user
    return user
