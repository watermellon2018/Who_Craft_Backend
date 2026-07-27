from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from w_craft_back.profile.models import UserProfile
from .models import ChannelSubscription


class SubscriptionError(Exception):
    """Domain error raised by SubscriptionService."""


class SelfSubscriptionError(SubscriptionError):
    pass


class TargetNotFoundError(SubscriptionError):
    pass


class SubscriptionNotFoundError(SubscriptionError):
    pass


@dataclass
class SubscriptionState:
    target_user_id: int
    is_subscribed: bool
    is_favorite: bool
    notifications_enabled: bool


def _ensure_target(current_user: User, target_user_id: int) -> User:
    if current_user.id == target_user_id:
        raise SelfSubscriptionError('cannot subscribe to yourself')
    try:
        target = User.objects.get(pk=target_user_id, is_active=True)
    except User.DoesNotExist:
        raise TargetNotFoundError('target user does not exist or is inactive')
    return target


def _ensure_profile(user: User) -> UserProfile:
    """Ensure ``UserProfile`` exists and return it locked for update.

    Called from inside ``@transaction.atomic`` blocks, so the row stays locked
    until commit. Two concurrent subscribe() calls thus serialize on the
    counter row instead of racing to double-increment.
    """
    UserProfile.objects.get_or_create(user=user)
    return UserProfile.objects.select_for_update().get(user=user)


@transaction.atomic
def subscribe(current_user: User, target_user_id: int) -> SubscriptionState:
    """Create or restore a subscription. Counters update only on real state change."""
    target = _ensure_target(current_user, target_user_id)

    sub = (
        ChannelSubscription.objects
        .select_for_update()
        .filter(subscriber=current_user, subscribed_to=target)
        .order_by('-created_at')
        .first()
    )

    became_active = False
    if sub is None:
        sub = ChannelSubscription.objects.create(
            subscriber=current_user,
            subscribed_to=target,
            notifications_enabled=True,
            is_favorite=False,
        )
        became_active = True
    elif sub.deleted_at is not None:
        sub.deleted_at = None
        sub.notifications_enabled = True
        sub.is_favorite = False
        sub.save(update_fields=['deleted_at', 'notifications_enabled', 'is_favorite', 'updated_at'])
        became_active = True
    # else: already active — no counter change, idempotent.

    if became_active:
        _ensure_profile(current_user)
        _ensure_profile(target)
        UserProfile.objects.filter(user=target).update(subscribers_count=F('subscribers_count') + 1)
        UserProfile.objects.filter(user=current_user).update(subscriptions_count=F('subscriptions_count') + 1)

    return SubscriptionState(
        target_user_id=target.id,
        is_subscribed=True,
        is_favorite=sub.is_favorite,
        notifications_enabled=sub.notifications_enabled,
    )


@transaction.atomic
def unsubscribe(current_user: User, target_user_id: int) -> SubscriptionState:
    """Soft-delete an active subscription. No-op (raise) if no active subscription."""
    if current_user.id == target_user_id:
        raise SelfSubscriptionError('cannot unsubscribe from yourself')

    sub = (
        ChannelSubscription.objects
        .select_for_update()
        .filter(
            subscriber=current_user,
            subscribed_to_id=target_user_id,
            deleted_at__isnull=True,
        )
        .first()
    )
    if sub is None:
        raise SubscriptionNotFoundError('no active subscription to remove')

    now = timezone.now()
    sub.deleted_at = now
    sub.is_favorite = False
    sub.notifications_enabled = False
    sub.save(update_fields=['deleted_at', 'is_favorite', 'notifications_enabled', 'updated_at'])

    UserProfile.objects.filter(user_id=target_user_id).update(
        subscribers_count=Greatest(F('subscribers_count') - 1, Value(0)),
    )
    UserProfile.objects.filter(user=current_user).update(
        subscriptions_count=Greatest(F('subscriptions_count') - 1, Value(0)),
    )

    return SubscriptionState(
        target_user_id=target_user_id,
        is_subscribed=False,
        is_favorite=False,
        notifications_enabled=False,
    )


@transaction.atomic
def update_settings(
    current_user: User,
    target_user_id: int,
    is_favorite: Optional[bool] = None,
    notifications_enabled: Optional[bool] = None,
) -> SubscriptionState:
    """Update is_favorite / notifications_enabled on an active subscription."""
    sub = (
        ChannelSubscription.objects
        .select_for_update()
        .filter(
            subscriber=current_user,
            subscribed_to_id=target_user_id,
            deleted_at__isnull=True,
        )
        .first()
    )
    if sub is None:
        raise SubscriptionNotFoundError('no active subscription found')

    fields = []
    if is_favorite is not None:
        sub.is_favorite = bool(is_favorite)
        fields.append('is_favorite')
    if notifications_enabled is not None:
        sub.notifications_enabled = bool(notifications_enabled)
        fields.append('notifications_enabled')
    if fields:
        fields.append('updated_at')
        sub.save(update_fields=fields)

    return SubscriptionState(
        target_user_id=target_user_id,
        is_subscribed=True,
        is_favorite=sub.is_favorite,
        notifications_enabled=sub.notifications_enabled,
    )


# ---------- Listing / search ----------

def _normalize_query(raw: str) -> str:
    if raw is None:
        return ''
    q = raw.strip()
    if q.startswith('@'):
        q = q[1:]
    return q.lower()


_MAX_PAGE_LIMIT = 100


def _clamp_page(limit: int, offset: int) -> tuple[int, int]:
    """Normalize pagination args. Negative offsets must NOT wrap from the end."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, _MAX_PAGE_LIMIT))
    offset = max(0, offset)
    return limit, offset


def list_my_subscriptions(current_user: User, limit: int, offset: int) -> dict:
    """List active subscriptions of current_user, favorites first, newest first."""
    limit, offset = _clamp_page(limit, offset)
    qs = (
        ChannelSubscription.objects
        .filter(subscriber=current_user, deleted_at__isnull=True, subscribed_to__is_active=True)
        .select_related('subscribed_to__profile')
        .order_by('-is_favorite', '-created_at')
    )
    total = qs.count()
    favorite_count = qs.filter(is_favorite=True).count()
    items = list(qs[offset:offset + limit])
    return {
        'items': [_serialize_subscription_row(s) for s in items],
        'total': total,
        'favoriteCount': favorite_count,
        'limit': limit,
        'offset': offset,
    }


def search_channels(current_user: User, query: str, limit: int, offset: int) -> dict:
    """Trigram-backed user search excluding self and inactive users."""
    limit, offset = _clamp_page(limit, offset)
    q = _normalize_query(query)
    if not q:
        return {'items': [], 'total': 0, 'limit': limit, 'offset': offset}
    # Escape LIKE wildcards in user input so a query like "foo%" matches
    # literally instead of as a pattern.
    like_pat = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    like = f'%{like_pat}%'

    from django.db import connection
    sql = """
        SELECT
            u.id,
            COALESCE(NULLIF(p.display_name, ''), u.username) AS display_name,
            p.public_username AS username,
            p.avatar AS avatar_path,
            COALESCE(p.subscribers_count, 0) AS subscribers_count,
            CASE WHEN s.id IS NULL THEN false ELSE true END AS is_subscribed,
            COALESCE(s.notifications_enabled, false) AS notifications_enabled,
            COALESCE(s.is_favorite, false) AS is_favorite
        FROM auth_user u
        LEFT JOIN user_profiles p ON p.user_id = u.id
        LEFT JOIN channel_subscriptions s
            ON s.subscribed_to_user_id = u.id
           AND s.subscriber_user_id = %s
           AND s.deleted_at IS NULL
        WHERE u.is_active = true
          AND u.id <> %s
          AND p.public_username IS NOT NULL
          AND (
              p.public_username ILIKE %s ESCAPE '\\'
              OR p.display_name ILIKE %s ESCAPE '\\'
          )
        ORDER BY
            COALESCE(s.is_favorite, false) DESC,
            GREATEST(
                similarity(p.public_username, %s),
                similarity(p.display_name, %s)
            ) DESC,
            COALESCE(p.subscribers_count, 0) DESC,
            p.display_name ASC
        LIMIT %s OFFSET %s
    """
    params = [current_user.id, current_user.id, like, like, q, q, limit, offset]

    with connection.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    items = []
    for row in rows:
        items.append({
            'id': row[0],
            'displayName': row[1] or '',
            'username': row[2],
            'avatarUrl': _build_avatar_url(row[3]),
            'subscribersCount': int(row[4] or 0),
            'isSubscribed': bool(row[5]),
            'notificationsEnabled': bool(row[6]),
            'isFavorite': bool(row[7]),
        })
    return {
        'items': items,
        'total': len(items),
        'limit': limit,
        'offset': offset,
    }


def list_subscribers(target_user_id: int, limit: int, offset: int) -> dict:
    limit, offset = _clamp_page(limit, offset)
    qs = (
        ChannelSubscription.objects
        .filter(subscribed_to_id=target_user_id, deleted_at__isnull=True, subscriber__is_active=True)
        .select_related('subscriber__profile')
        .order_by('-created_at')
    )
    total = qs.count()
    items = list(qs[offset:offset + limit])
    return {
        'items': [_serialize_user_brief(s.subscriber) for s in items],
        'total': total,
        'limit': limit,
        'offset': offset,
    }


def list_user_subscriptions(user_id: int, limit: int, offset: int) -> dict:
    limit, offset = _clamp_page(limit, offset)
    qs = (
        ChannelSubscription.objects
        .filter(subscriber_id=user_id, deleted_at__isnull=True, subscribed_to__is_active=True)
        .select_related('subscribed_to__profile')
        .order_by('-is_favorite', '-created_at')
    )
    total = qs.count()
    items = list(qs[offset:offset + limit])
    return {
        'items': [_serialize_user_brief(s.subscribed_to) for s in items],
        'total': total,
        'limit': limit,
        'offset': offset,
    }


# ---------- Serializers (kept here to mirror Django service style of the project) ----------

def _build_avatar_url(image_field_value) -> Optional[str]:
    if not image_field_value:
        return None
    from w_craft_back.storage_gateway import signed_media_url

    return signed_media_url(str(image_field_value))


def _serialize_subscription_row(sub: ChannelSubscription) -> dict:
    user = sub.subscribed_to
    profile = getattr(user, 'profile', None)
    display_name = (profile.display_name if profile and profile.display_name else user.username) or ''
    username = profile.public_username if profile else None
    avatar = _build_avatar_url(profile.avatar.name if (profile and profile.avatar) else None)
    subscribers = profile.subscribers_count if profile else 0
    return {
        'id': user.id,
        'displayName': display_name,
        'username': username,
        'avatarUrl': avatar,
        'subscribersCount': subscribers,
        'isSubscribed': True,
        'notificationsEnabled': sub.notifications_enabled,
        'isFavorite': sub.is_favorite,
    }


def _serialize_user_brief(user: User) -> dict:
    profile = getattr(user, 'profile', None)
    display_name = (profile.display_name if profile and profile.display_name else user.username) or ''
    username = profile.public_username if profile else None
    avatar = _build_avatar_url(profile.avatar.name if (profile and profile.avatar) else None)
    subscribers = profile.subscribers_count if profile else 0
    return {
        'id': user.id,
        'displayName': display_name,
        'username': username,
        'avatarUrl': avatar,
        'subscribersCount': subscribers,
    }
