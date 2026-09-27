"""Owner-only connection mutations, serialized against account closure."""

from typing import Any
from uuid import UUID

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .catalog import model_catalog, provider_catalog, validate_route
from .crypto import encrypt_secret, storage_available
from .errors import PersonalAPIError
from .models import MODALITIES, PersonalAPIConnection, PersonalAPIPreference


def require_active_user(user: User, *, lock: bool = False) -> User:
    queryset = User.objects.select_for_update() if lock else User.objects
    active = queryset.filter(pk=getattr(user, 'pk', None), is_active=True).first()
    if active is None:
        raise PersonalAPIError('ACCOUNT_INACTIVE', 'Account is unavailable.', 403)
    return active


def connection_data(connection: PersonalAPIConnection) -> dict:
    return {
        'id': str(connection.pk), 'name': connection.name,
        'provider': connection.provider, 'keyHint': connection.key_hint,
        'isActive': connection.is_active, 'version': connection.version,
        'createdAt': connection.created_at.isoformat(),
        'updatedAt': connection.updated_at.isoformat(),
    }


def preference_data(
    modality: str, preference: PersonalAPIPreference | None = None,
) -> dict:
    return {
        'modality': modality,
        'source': preference.source if preference else 'platform',
        'routes': preference.routes if preference else [],
    }


def get_settings(user: User) -> dict:
    from .access import has_paid_subscription

    user = require_active_user(user)
    models = model_catalog()
    preferences = {
        row.modality: row for row in PersonalAPIPreference.objects.filter(user=user)
    }
    return {
        'connections': [connection_data(connection) for connection in
                        PersonalAPIConnection.objects.filter(
                            user=user, revoked_at__isnull=True,
                        )],
        'preferences': [preference_data(modality, preferences.get(modality))
                        for modality in MODALITIES],
        'providers': provider_catalog(models), 'models': models,
        'hasPaidSubscription': has_paid_subscription(user),
        'storageAvailable': storage_available(),
    }


def _owned_connection(user: User, connection_id: UUID) -> PersonalAPIConnection:
    connection = PersonalAPIConnection.objects.select_for_update().filter(
        user=user, pk=connection_id, revoked_at__isnull=True,
    ).first()
    if connection is None:
        raise PersonalAPIError(
            'PERSONAL_API_CONNECTION_NOT_FOUND', 'API connection was not found.', 404,
        )
    return connection


@transaction.atomic
@sensitive_variables('apiKey')
def create_connection(
    user: User, *, name: str, provider: str, apiKey: str,
) -> dict:
    user = require_active_user(user, lock=True)
    if PersonalAPIConnection.objects.filter(
        user=user, revoked_at__isnull=True,
    ).count() >= 20:
        raise PersonalAPIError(
            'PERSONAL_API_CONNECTION_LIMIT', 'At most 20 API connections are allowed.',
        )
    connection = PersonalAPIConnection(user=user, name=name, provider=provider)
    connection.encrypted_key = encrypt_secret(connection, apiKey)
    connection.key_hint = f'••••{apiKey[-4:]}'
    connection.save()
    return connection_data(connection)


@transaction.atomic
@sensitive_variables('changes', 'secret')
def update_connection(user: User, connection_id: UUID, **changes: Any) -> dict:
    user = require_active_user(user, lock=True)
    connection = _owned_connection(user, connection_id)
    if 'name' in changes:
        connection.name = changes['name']
    if 'apiKey' in changes:
        secret = changes['apiKey']
        connection.encrypted_key = encrypt_secret(connection, secret)
        connection.key_hint = f'••••{secret[-4:]}'
        connection.version += 1
    if 'isActive' in changes and changes['isActive'] != connection.is_active:
        connection.is_active = changes['isActive']
        # A disable/re-enable cycle must not restore an already queued grant.
        connection.version += 1
    connection.updated_at = timezone.now()
    connection.save()
    return connection_data(connection)


@transaction.atomic
def revoke_connection(user: User, connection_id: UUID) -> None:
    user = require_active_user(user, lock=True)
    connection = _owned_connection(user, connection_id)
    connection.encrypted_key = ''
    connection.key_hint = ''
    connection.is_active = False
    connection.revoked_at = connection.updated_at = timezone.now()
    connection.version += 1
    connection.save()
    for preference in PersonalAPIPreference.objects.filter(user=user):
        routes = [route for route in preference.routes
                  if route.get('connectionId') != str(connection.pk)]
        if routes != preference.routes:
            preference.routes = routes
            preference.updated_at = timezone.now()
            preference.save(update_fields=['routes', 'updated_at'])


@transaction.atomic
def save_preference(
    user: User, modality: str, *, source: str, routes: list[dict],
) -> dict:
    user = require_active_user(user, lock=True)
    if modality not in MODALITIES:
        raise PersonalAPIError(
            'PERSONAL_API_MODALITY_UNSUPPORTED', 'This modality is not supported.',
        )
    if source not in {'platform', 'personal'} or len(routes) > 8:
        raise PersonalAPIError(
            'PERSONAL_API_INVALID_PREFERENCE', 'Invalid preferences.',
        )
    validated = []
    seen = set()
    for route in routes:
        connection = _owned_connection(user, route['connectionId'])
        model_key = route['modelKey']
        pair = (str(connection.pk), model_key)
        if pair in seen:
            raise PersonalAPIError(
                'PERSONAL_API_DUPLICATE_ROUTE', 'The same route was added twice.',
            )
        validate_route(modality, connection.provider, model_key)
        seen.add(pair)
        validated.append({'connectionId': pair[0], 'modelKey': model_key})
    preference, _ = PersonalAPIPreference.objects.update_or_create(
        user=user, modality=modality,
        defaults={
            'source': source, 'routes': validated, 'updated_at': timezone.now(),
        },
    )
    return preference_data(modality, preference)


def scrub_user_connections(user: User) -> None:
    """Called within account closure after acquiring its user-row lock."""
    PersonalAPIConnection.objects.filter(user=user).update(
        encrypted_key='', key_hint='', name='', is_active=False,
        revoked_at=timezone.now(), updated_at=timezone.now(), version=F('version') + 1,
    )
    PersonalAPIPreference.objects.filter(user=user).delete()
