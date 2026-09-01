from __future__ import annotations

import uuid
from typing import Iterable

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from w_craft_back.storage_gateway import (
    MediaTooLarge,
    StorageGatewayError,
    UnsupportedMedia,
    delete_storage_key,
    store_image_upload,
)

from .models import (
    Interest,
    UserAsset,
    UserInterest,
    UserProfile,
    UserSocialLink,
)


AVATAR_MAX_BYTES = 5 * 1024 * 1024
COVER_MAX_BYTES = 10 * 1024 * 1024


class FileTooLarge(MediaTooLarge):
    """Profile upload exceeded its configured byte limit."""


class UnsupportedMediaType(UnsupportedMedia):
    """Profile upload is not a decodable supported image."""


class InvalidCurrentPassword(Exception):
    """The account-closing password confirmation did not match."""


class AccountHasOwnedProjects(Exception):
    """Account closure is blocked until all owned projects are transferred."""

    def __init__(self, owned_projects_count: int) -> None:
        self.owned_projects_count = owned_projects_count
        super().__init__('account owns projects')


class InactiveAccount(Exception):
    """A stale authenticated request reached a deactivated account."""


def lock_active_user(user: User) -> User:
    """Lock one user row and reject account mutations after deactivation."""

    locked_user = (
        User.objects.select_for_update()
        .filter(pk=user.pk)
        .first()
    )
    if locked_user is None or not locked_user.is_active:
        raise InactiveAccount()
    return locked_user


def _normalize_interest_name(name: str) -> tuple[str, str]:
    clean = name.strip()
    slug = slugify(clean, allow_unicode=True) or clean.lower()
    return clean, slug


def _resolve_interest(clean: str, slug: str) -> Interest:
    existing = Interest.objects.filter(name__iexact=clean).first()
    if existing is not None:
        return existing
    existing = Interest.objects.filter(slug=slug).first()
    if existing is not None:
        return existing
    try:
        return Interest.objects.create(name=clean, slug=slug)
    except Exception:
        # race or unicode-collision fallback
        return (
            Interest.objects.filter(name__iexact=clean).first()
            or Interest.objects.get(slug=slug)
        )


@transaction.atomic
def replace_user_interests(user, names: Iterable[str]) -> list[Interest]:
    user = lock_active_user(user)
    seen_keys: set[str] = set()
    interests: list[Interest] = []
    for raw in names:
        if not isinstance(raw, str) or not raw.strip():
            continue
        clean, slug = _normalize_interest_name(raw)
        key = clean.casefold()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        interests.append(_resolve_interest(clean, slug))

    UserInterest.objects.filter(user=user).delete()
    UserInterest.objects.bulk_create(
        [UserInterest(user=user, interest=i) for i in interests]
    )
    return interests


@transaction.atomic
def replace_user_socials(user, items: Iterable[dict]) -> list[UserSocialLink]:
    user = lock_active_user(user)
    UserSocialLink.objects.filter(user=user).delete()
    rows: list[UserSocialLink] = []
    for idx, item in enumerate(items):
        rows.append(
            UserSocialLink(
                user=user,
                platform=item['platform'],
                url=item['url'],
                display_order=idx,
            )
        )
    UserSocialLink.objects.bulk_create(rows)
    return rows


@transaction.atomic
def close_user_account(user: User, current_password: str) -> None:
    """Deactivate and anonymize an account while preserving audit history."""

    from w_craft_back.auth.models import UserKey
    from w_craft_back.movie.project.comment_models import VideoShotComment
    from w_craft_back.movie.project.dashboard_models import ProjectMember
    from w_craft_back.movie.project.models import Project
    from w_craft_back.movie.project.team_models import ProjectInvitation
    from w_craft_back.notifications.models import (
        EmailNotificationDelivery,
        Notification,
    )
    from w_craft_back.subscriptions.services import remove_user_subscriptions

    locked_user = User.objects.select_for_update().get(pk=user.pk)
    if not locked_user.check_password(current_password):
        raise InvalidCurrentPassword()

    owned_projects_count = Project.objects.filter(owner=locked_user).count()
    if owned_projects_count:
        raise AccountHasOwnedProjects(owned_projects_count)

    # Mark the account inactive before removing its personal relations. The
    # change becomes visible atomically with the rest of the closure.
    locked_user.is_active = False
    locked_user.save(update_fields=['is_active'])

    remove_user_subscriptions(locked_user)
    ProjectMember.objects.filter(user=locked_user).delete()
    ProjectInvitation.objects.filter(invited_user=locked_user).delete()
    ProjectInvitation.objects.filter(invited_by=locked_user).update(
        invited_by=None,
    )
    ProjectInvitation.objects.filter(accepted_by=locked_user).update(
        accepted_by=None,
    )
    VideoShotComment.objects.filter(author=locked_user).delete()
    Notification.objects.filter(recipient=locked_user).delete()
    EmailNotificationDelivery.objects.filter(recipient=locked_user).delete()

    # Delete the profile first so UserAsset deletion can remove media after the
    # final profile reference disappears. Storage cleanup is scheduled by the
    # existing post-delete signals after this transaction commits.
    UserProfile.objects.filter(user=locked_user).delete()
    UserAsset.objects.filter(user=locked_user).delete()
    UserInterest.objects.filter(user=locked_user).delete()
    UserSocialLink.objects.filter(user=locked_user).delete()
    UserKey.objects.filter(user=locked_user).delete()

    locked_user.username = (
        f'deleted_user_{locked_user.pk}_{uuid.uuid4().hex}'
    )
    locked_user.email = ''
    locked_user.first_name = ''
    locked_user.last_name = ''
    locked_user.last_login = None
    locked_user.is_staff = False
    locked_user.is_superuser = False
    locked_user.set_unusable_password()
    locked_user.save(
        update_fields=[
            'username',
            'email',
            'first_name',
            'last_name',
            'last_login',
            'is_active',
            'is_staff',
            'is_superuser',
            'password',
        ],
    )
    locked_user.groups.clear()
    locked_user.user_permissions.clear()


def save_uploaded_image(
    user: User,
    django_file,
    asset_type: str,
) -> tuple[UserAsset, UserProfile]:
    """Store profile media while serialized against account closure."""

    if asset_type == UserAsset.AVATAR:
        max_bytes = AVATAR_MAX_BYTES
        field_name = 'avatar'
        link_field = 'avatar_asset'
    elif asset_type == UserAsset.COVER:
        max_bytes = COVER_MAX_BYTES
        field_name = 'cover'
        link_field = 'cover_asset'
    else:
        raise ValueError(f'unsupported asset_type: {asset_type}')

    stored = None
    try:
        with transaction.atomic():
            user = lock_active_user(user)
            try:
                stored = store_image_upload(
                    django_file,
                    namespace=f'profiles/{user.id}/{asset_type}',
                    max_bytes=max_bytes,
                )
            except MediaTooLarge as exc:
                raise FileTooLarge(exc.message) from exc
            except StorageGatewayError as exc:
                raise UnsupportedMediaType(exc.message) from exc

            profile, _ = UserProfile.objects.select_for_update().get_or_create(
                user=user
            )
            setattr(profile, field_name, stored.storage_key)
            profile.save(update_fields=[field_name])

            asset = UserAsset.objects.create(
                user=user,
                type=asset_type,
                storage_key=stored.storage_key,
                url=None,
                mime_type=stored.mime_type,
                size_bytes=stored.size_bytes,
                width=stored.width,
                height=stored.height,
            )
            UserAsset.objects.filter(
                user=user,
                type=asset_type,
                deleted_at__isnull=True,
            ).exclude(pk=asset.pk).update(deleted_at=timezone.now())
            setattr(profile, link_field, asset)
            profile.save(update_fields=[link_field])
    except Exception:
        if stored is not None:
            delete_storage_key(stored.storage_key)
        raise
    return asset, profile


def delete_image(user: User, asset_type: str) -> UserProfile:
    """Detach profile media; cleanup runs only after transaction commit."""

    if asset_type == UserAsset.AVATAR:
        field_name = 'avatar'
        link_field = 'avatar_asset'
    elif asset_type == UserAsset.COVER:
        field_name = 'cover'
        link_field = 'cover_asset'
    else:
        raise ValueError(f'unsupported asset_type: {asset_type}')

    with transaction.atomic():
        user = lock_active_user(user)
        profile, _ = (
            UserProfile.objects.select_for_update().get_or_create(
                user=user,
            )
        )
        setattr(profile, field_name, None)
        setattr(profile, link_field, None)
        profile.save(update_fields=[field_name, link_field])
        UserAsset.objects.filter(
            user=user,
            type=asset_type,
            deleted_at__isnull=True,
        ).update(deleted_at=timezone.now())
        # The pre_save hook records the replaced key and performs a
        # reference-aware delete after this transaction commits.
        return profile
