from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.services.errors import PermissionDeniedError, ValidationError
from w_craft_back.movie.project.models import Project


def get_user_from_request(request):
    token = request.GET.get("token_user")
    if not token and hasattr(request, "data"):
        token = request.data.get("token_user") or request.data.get("user_id")
    if not token and request.user and request.user.is_authenticated:
        return UserKey.objects.get(user=request.user)
    if not token:
        raise ValidationError("token_user is required.")
    try:
        return UserKey.objects.get(key=token)
    except UserKey.DoesNotExist as exc:
        raise PermissionDeniedError("Invalid user token.") from exc


def get_owned_project(user, project_id):
    try:
        return Project.objects.get(id=project_id, user=user)
    except Project.DoesNotExist as exc:
        raise PermissionDeniedError() from exc

