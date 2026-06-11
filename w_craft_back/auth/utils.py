import logging
import uuid
from typing import Optional

from rest_framework.exceptions import AuthenticationFailed

from w_craft_back.auth.models import UserKey

logger = logging.getLogger(__name__)


def extract_user_token(request) -> Optional[str]:
    """Return the user token from the ``X-User-Token`` header or request body.

    Preference order:
    1. ``X-User-Token`` header  (preferred — does not appear in logs/Referer)
    2. JSON/form body ``token_user`` (legacy POST clients only)

    Query-string ``?token_user=`` is no longer accepted: the token would leak
    into access logs, browser history, and Referer headers.
    """
    header_token = request.META.get('HTTP_X_USER_TOKEN')
    if header_token:
        return header_token.strip()

    data = getattr(request, 'data', None)
    if isinstance(data, dict):
        token = data.get('token_user')
        if token:
            return str(token).strip()

    if _token_in_query_string(request):
        logger.warning(
            "token_user supplied via query string at %s — clients must use the X-User-Token header",
            getattr(request, 'path', '<unknown>'),
        )

    return None


def _token_in_query_string(request) -> bool:
    if hasattr(request, 'query_params'):
        return bool(request.query_params.get('token_user'))
    if hasattr(request, 'GET'):
        return bool(request.GET.get('token_user'))
    return False


def resolve_user_key(request) -> UserKey:
    """Resolve the calling ``UserKey`` or raise ``AuthenticationFailed`` (401)."""
    token = extract_user_token(request)
    if not token:
        raise AuthenticationFailed('Authentication token missing')

    try:
        uuid.UUID(str(token))
    except (ValueError, TypeError):
        raise AuthenticationFailed('Invalid authentication token')

    try:
        return UserKey.objects.select_related('user').get(key=token)
    except UserKey.DoesNotExist:
        raise AuthenticationFailed('Invalid authentication token')
