"""Authenticated encryption with a dedicated web/worker deployment key."""

import json
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from .errors import PersonalAPIError, unavailable_connection

if TYPE_CHECKING:
    from .models import PersonalAPIConnection


def _cipher() -> Fernet:
    try:
        key = getattr(settings, 'PERSONAL_API_ENCRYPTION_KEY', '')
        return Fernet(key.encode('ascii'))
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise PersonalAPIError(
            'PERSONAL_API_STORAGE_UNAVAILABLE',
            'Personal API storage is not configured. Contact the administrator.',
            503,
        ) from None


def storage_available() -> bool:
    try:
        _cipher()
    except PersonalAPIError:
        return False
    return True


@sensitive_variables('secret', 'payload')
def encrypt_secret(connection: 'PersonalAPIConnection', secret: str) -> str:
    payload = json.dumps({
        'connectionId': str(connection.pk), 'ownerId': connection.user_id,
        'provider': connection.provider, 'secret': secret,
    }, separators=(',', ':')).encode('utf-8')
    return _cipher().encrypt(payload).decode('ascii')


@sensitive_variables('payload')
def decrypt_secret(connection: 'PersonalAPIConnection') -> str:
    try:
        payload = json.loads(_cipher().decrypt(
            connection.encrypted_key.encode('ascii'),
        ))
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        raise unavailable_connection() from None
    if (
        not isinstance(payload, dict)
        or payload.get('connectionId') != str(connection.pk)
        or payload.get('ownerId') != connection.user_id
        or payload.get('provider') != connection.provider
        or not isinstance(payload.get('secret'), str)
        or not payload['secret']
    ):
        raise unavailable_connection()
    return payload['secret']
