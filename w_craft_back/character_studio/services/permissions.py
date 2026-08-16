from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.services.errors import (
    PermissionDeniedError,
    ValidationError,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.policy import Action


def get_user_from_request(request):
    """Return the authenticated UserKey credential for service compatibility."""
    request_auth = getattr(request, "auth", None)
    if isinstance(request_auth, UserKey):
        return request_auth

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise ValidationError(
            "Authentication token is required (send X-User-Token header)."
        )
    try:
        return UserKey.objects.select_related("user").get(user=user)
    except UserKey.DoesNotExist as exc:
        raise PermissionDeniedError("Invalid user token.") from exc


def get_project_for_action(user, project_id, action: Action):
    """Return a project only when ``user`` may perform ``action``."""
    from w_craft_back.movie.project import policy

    try:
        project = Project.objects.select_related("owner").get(id=project_id)
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


def _auth_user(user):
    """Resolve the Django User behind a principal that may be a UserKey."""
    if isinstance(user, UserKey):
        return user.user
    return user
