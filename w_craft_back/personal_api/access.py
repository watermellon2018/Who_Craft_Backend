"""Resolve a payer once and recheck personal grants before external calls."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID

from django.contrib.auth.models import User
from django.core import signing
from django.db.models import F
from django.utils import timezone

from .catalog import validate_route
from .crypto import decrypt_secret, storage_available
from .errors import PersonalAPIError, unavailable_connection
from .models import MODALITIES, PersonalAPIConnection, PersonalAPIPreference
from .services import require_active_user, validate_connection_route


AccessError = PersonalAPIError
APPROVAL_MAX_AGE_SECONDS = 300
_APPROVAL_SALT = 'craft.personal-api.generation-access.v1'


def has_paid_subscription(user: User) -> bool:
    """Future billing integration; social follows and wallet funds grant nothing."""
    return False


def generation_source(user: User, modality: str) -> str:
    preference = PersonalAPIPreference.objects.filter(
        user=user, modality=modality,
    ).values_list('source', flat=True).first()
    return preference or 'platform'


def _subscription_required() -> PersonalAPIError:
    return PersonalAPIError(
        'CRAFT_SUBSCRIPTION_REQUIRED',
        'Paid Craft models require a subscription. Use a personal API connection.',
        403,
    )


def resolve_generation_access(
    user: User,
    modality: str,
    *,
    source: str | None = None,
    model_key: str | None = None,
    operation: str = 'generate',
    free_platform: bool = False,
    approval_token: str | None = None,
) -> dict[str, Any]:
    """Freeze safe connection references; never infer free access from price.

    ``free_platform`` is exclusively for trusted callers whose exact local or
    external route is explicitly known free. It must never come from a request.
    An explicit model starts at its first configured route, retaining only the
    following routes already approved by the user as ordered alternatives.
    """
    user = require_active_user(user)
    if modality not in MODALITIES:
        raise PersonalAPIError(
            'PERSONAL_API_MODALITY_UNSUPPORTED', 'This modality is not supported.',
        )
    if source is not None and source not in ('platform', 'personal'):
        raise PersonalAPIError(
            'GENERATION_SOURCE_INVALID', 'Choose API Craft or a personal API.',
        )
    if approval_token:
        return _resolve_approved_access(
            user, modality, source=source, model_key=model_key,
            operation=operation, approval_token=approval_token,
        )
    free_platform = free_platform is True
    chosen_source = 'platform' if free_platform else source or generation_source(
        user, modality,
    )
    snapshot: dict[str, Any] = {
        'version': 1, 'source': chosen_source, 'modality': modality,
        'operation': operation, 'ownerId': user.pk, 'candidates': [],
    }
    if chosen_source == 'platform':
        if not free_platform and not has_paid_subscription(user):
            raise _subscription_required()
        snapshot['freePlatform'] = bool(free_platform)
        return snapshot
    if not storage_available():
        raise PersonalAPIError(
            'PERSONAL_API_STORAGE_UNAVAILABLE', 'Personal API storage is unavailable.',
            503,
        )
    preference = PersonalAPIPreference.objects.filter(
        user=user, modality=modality,
    ).first()
    routes = preference.routes if preference else []
    reached_selected_model = not model_key
    for route in routes:
        if not reached_selected_model and route.get('modelKey') == model_key:
            reached_selected_model = True
        if not reached_selected_model:
            continue
        connection = PersonalAPIConnection.objects.filter(
            user=user, pk=route.get('connectionId'), is_active=True,
            revoked_at__isnull=True,
        ).first()
        if connection is None:
            continue
        try:
            validate_connection_route(
                modality, connection, route['modelKey'], operation,
            )
        except PersonalAPIError:
            continue
        snapshot['candidates'].append({
            'connectionId': str(connection.pk),
            'connectionVersion': connection.version,
            'connectionName': connection.name,
            'provider': connection.provider, 'modelKey': route['modelKey'],
        })
    if not snapshot['candidates']:
        raise PersonalAPIError(
            'PERSONAL_API_NOT_CONFIGURED',
            'No personal API connection supports this operation. '
            'Configure this modality.',
            403,
        )
    return snapshot


def sign_generation_access(snapshot: Mapping) -> str:
    """Approve an immutable personal route chain without exposing any API key."""
    if snapshot.get('source') != 'personal':
        return ''
    # Only serialize the allowlisted reference fields, even if an internal caller
    # accidentally attached provider/client data to an otherwise valid snapshot.
    payload = {
        name: snapshot[name] for name in
        ('version', 'source', 'modality', 'operation', 'ownerId')
    }
    payload['candidates'] = [{
        name: candidate[name] for name in
        ('connectionId', 'connectionVersion', 'provider', 'modelKey')
    } | {'connectionName': candidate.get('connectionName', '')}
        for candidate in snapshot['candidates']]
    validate_generation_access(payload)
    return signing.dumps(payload, salt=_APPROVAL_SALT, compress=True)


def _approval_mismatch() -> PersonalAPIError:
    return PersonalAPIError(
        'GENERATION_APPROVAL_MISMATCH',
        'The approved API route no longer matches this request. Review it again.',
        409,
    )


def _resolve_approved_access(
    user: User, modality: str, *, source: str | None,
    model_key: str | None, operation: str, approval_token: str,
) -> dict[str, Any]:
    if not isinstance(approval_token, str) or len(approval_token) > 16384:
        raise PersonalAPIError(
            'GENERATION_APPROVAL_INVALID', 'The generation approval is invalid.',
        )
    try:
        snapshot = signing.loads(
            approval_token, salt=_APPROVAL_SALT, max_age=APPROVAL_MAX_AGE_SECONDS,
        )
    except signing.SignatureExpired:
        raise PersonalAPIError(
            'GENERATION_APPROVAL_EXPIRED',
            'The generation approval has expired. Review the API route again.',
            409,
        ) from None
    except (signing.BadSignature, ValueError, TypeError):
        raise PersonalAPIError(
            'GENERATION_APPROVAL_INVALID', 'The generation approval is invalid.',
        ) from None
    if (
        not isinstance(snapshot, dict) or snapshot.get('source') != 'personal'
        or snapshot.get('ownerId') != user.pk or snapshot.get('modality') != modality
        or (source is not None and snapshot['source'] != source)
    ):
        raise _approval_mismatch()
    candidates = snapshot.get('candidates')
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
        raise _approval_mismatch()
    primary = candidates[0]
    if (
        not isinstance(primary, Mapping)
        or (model_key and primary.get('modelKey') != model_key)
    ):
        raise _approval_mismatch()
    try:
        # Check every pinned grant before enqueue. A changed key requires a new
        # review, rather than choosing another payer after the confirmation.
        for candidate in candidates:
            credential_for_candidate(snapshot, candidate, user=user)
        validate_route(modality, primary['provider'], primary['modelKey'], operation)
    except (PersonalAPIError, KeyError):
        raise _approval_mismatch() from None
    approved = [deepcopy(primary)]
    for candidate in candidates[1:]:
        try:
            validate_route(
                modality, candidate['provider'], candidate['modelKey'], operation,
            )
        except PersonalAPIError:
            # Actual inputs may require references/editing; an approved backup
            # can be removed for incompatibility, but a new route is never added.
            continue
        approved.append(deepcopy(candidate))
    return {**snapshot, 'operation': operation, 'candidates': approved}


def _snapshot_owner(snapshot: Mapping, user: User | None = None) -> User:
    if (
        not isinstance(snapshot, Mapping) or snapshot.get('version') != 1
        or isinstance(snapshot.get('version'), bool)
        or snapshot.get('source') not in ('personal', 'platform')
        or snapshot.get('modality') not in MODALITIES
        or not isinstance(snapshot.get('ownerId'), int)
        or isinstance(snapshot.get('ownerId'), bool)
    ):
        raise unavailable_connection()
    owner_id = snapshot['ownerId']
    if user is not None and getattr(user, 'pk', None) != owner_id:
        raise unavailable_connection()
    owner = User.objects.filter(pk=owner_id, is_active=True).first()
    if owner is None:
        raise unavailable_connection()
    return owner


def _candidate_connection(
    snapshot: Mapping, candidate: Mapping, user: User | None = None,
) -> PersonalAPIConnection:
    owner = _snapshot_owner(snapshot, user)
    candidates = snapshot.get('candidates')
    if (
        snapshot.get('source') != 'personal' or not isinstance(candidate, Mapping)
        or not isinstance(candidates, list) or len(candidates) > 8
        or candidate not in candidates
        or not isinstance(candidate.get('connectionVersion'), int)
        or isinstance(candidate.get('connectionVersion'), bool)
    ):
        raise unavailable_connection()
    try:
        connection_id = UUID(str(candidate.get('connectionId', '')))
    except (ValueError, TypeError, AttributeError):
        raise unavailable_connection() from None
    connection = PersonalAPIConnection.objects.filter(
        pk=connection_id, user=owner, user__is_active=True,
        is_active=True, revoked_at__isnull=True,
        version=candidate['connectionVersion'], provider=candidate.get('provider'),
    ).first()
    if connection is None:
        raise unavailable_connection()
    return connection


def credential_for_candidate(
    access_snapshot: Mapping, candidate: Mapping, *, user: User | None = None,
) -> str:
    """Recheck revocation/version/ownership immediately before a provider call."""
    return decrypt_secret(_candidate_connection(access_snapshot, candidate, user))


def validate_generation_access(
    snapshot: Mapping, *, user: User | None = None,
) -> None:
    owner = _snapshot_owner(snapshot, user)
    if snapshot['source'] == 'platform':
        if (
            snapshot.get('freePlatform') is not True
            and not has_paid_subscription(owner)
        ):
            raise _subscription_required()
        return
    candidates = snapshot.get('candidates')
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
        raise unavailable_connection()
    for candidate in candidates:
        try:
            credential_for_candidate(snapshot, candidate, user=owner)
        except PersonalAPIError as exc:
            if exc.code == 'PERSONAL_API_STORAGE_UNAVAILABLE':
                raise
            continue
        return
    raise unavailable_connection()


def reject_candidate(
    snapshot: Mapping, candidate: Mapping, *, reason: str,
) -> None:
    """Disable a definitively rejected key, never on timeout or model/quota errors."""
    if reason != 'authentication':
        return
    try:
        connection = _candidate_connection(snapshot, candidate)
    except PersonalAPIError:
        return
    PersonalAPIConnection.objects.filter(
        pk=connection.pk, version=candidate['connectionVersion'],
    ).update(is_active=False, version=F('version') + 1, updated_at=timezone.now())


def resolve_video_access(
    user: User, *, requested_duration: float, model_key: str | None = None,
    aspect_ratio: str = '16:9', resolution: str = '720p',
    has_start_frame: bool = True, has_end_frame: bool = False,
) -> dict[str, Any]:
    """Pin profile-only video routes and disclose the first compatible duration."""
    from w_craft_back.services.video_generation.contracts import VideoProviderError
    from w_craft_back.services.video_generation.registry import (
        compatible_model, is_configured, model_snapshot, nearest_duration,
        resolve_model,
    )

    user = require_active_user(user)
    source = generation_source(user, 'video')
    if source == 'personal' and model_key:
        raise PersonalAPIError(
            'VIDEO_MODEL_PROFILE_ONLY',
            'Personal video models are selected in the profile.',
        )
    access = resolve_generation_access(user, 'video')
    selected_model = None
    selected_duration = None
    candidates = []
    candidate_routes = []
    if source == 'platform':
        if not model_key:
            raise PersonalAPIError('VIDEO_MODEL_REQUIRED', 'Choose a video model.')
        route_candidates = [{'modelKey': model_key}]
    else:
        route_candidates = access['candidates']
    for candidate in route_candidates:
        try:
            model = resolve_model(candidate['modelKey'])
            if source == 'platform' and not is_configured(model):
                raise VideoProviderError('VIDEO_PROVIDER_NOT_CONFIGURED')
            if not compatible_model(
                model, aspect_ratio=aspect_ratio, resolution=resolution,
                has_start_frame=has_start_frame, has_end_frame=has_end_frame,
            ):
                continue
            duration = nearest_duration(
                model, requested_duration, resolution=resolution,
                has_end_frame=has_end_frame,
            )
        except VideoProviderError:
            continue
        if selected_model is None:
            selected_model = model
            selected_duration = duration
        if duration == selected_duration:
            candidates.append(candidate)
            candidate_routes.append({
                'modelKey': model.key, 'modelLabel': model.label,
                'provider': model.provider, 'durationSeconds': duration,
                'aspectRatio': aspect_ratio, 'resolution': resolution,
                'modelSpec': model_snapshot(model),
            })
    if selected_model is None:
        raise PersonalAPIError(
            'VIDEO_MODEL_INCOMPATIBLE',
            'No configured video model supports the selected frames and format.', 403,
        )
    if source == 'personal':
        access = {**access, 'candidates': candidates}
    return {
        'generationAccess': access, 'modelKey': selected_model.key,
        'modelLabel': selected_model.label, 'provider': selected_model.provider,
        'durationSeconds': selected_duration, 'aspectRatio': aspect_ratio,
        'resolution': resolution, 'modelSpec': model_snapshot(selected_model),
        'candidateRoutes': candidate_routes,
    }
