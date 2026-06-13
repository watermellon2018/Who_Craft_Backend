import uuid

from w_craft_back.auth.models import UserKey
from w_craft_back.auth.utils import extract_user_token
from w_craft_back.character_studio.services.errors import PermissionDeniedError, ValidationError
from w_craft_back.movie.project.models import Project


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
        raise ValidationError("Authentication token is required (send X-User-Token header).")
    try:
        uuid.UUID(str(token))
    except (ValueError, TypeError) as exc:
        raise PermissionDeniedError("Invalid user token.") from exc
    try:
        return UserKey.objects.select_related("user").get(key=token)
    except UserKey.DoesNotExist as exc:
        raise PermissionDeniedError("Invalid user token.") from exc


def get_owned_project(user, project_id):
    """Return the project if ``user`` (a UserKey) may access it.

    Collaboration-aware: access is granted to the legacy UserKey owner, the
    direct owner, AND any active ProjectMember — resolved through the central
    project policy. The historical name ``get_owned_project`` is kept so the
    many existing character_studio call sites stay unchanged, but it now means
    "a project this user has at least view access to".
    """
    from w_craft_back.movie.project import policy

    try:
        project = Project.objects.select_related("owner", "user").get(id=project_id)
    except Project.DoesNotExist as exc:
        raise PermissionDeniedError() from exc
    if not policy.can_view(_auth_user(user), project):
        raise PermissionDeniedError()
    return project


def require_project_edit(user, project_id):
    """Like :func:`get_owned_project` but requires content-edit permission.

    Use for character create/update/delete and generation launches so viewers
    are correctly rejected even on the legacy UserKey-scoped surface.
    """
    from w_craft_back.movie.project import policy

    try:
        project = Project.objects.select_related("owner", "user").get(id=project_id)
    except Project.DoesNotExist as exc:
        raise PermissionDeniedError() from exc
    if not policy.can_edit(_auth_user(user), project):
        raise PermissionDeniedError()
    return project


def _auth_user(user):
    """Resolve the Django User behind a principal that may be a UserKey."""
    # character_studio resolves principals as UserKey; the policy works on the
    # underlying auth User.
    if isinstance(user, UserKey):
        return user.user
    return user

