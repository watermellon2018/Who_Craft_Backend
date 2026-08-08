from __future__ import annotations

from typing import Iterable

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


def save_uploaded_image(user, django_file, asset_type: str) -> UserAsset:
    """Normalize an image before a short metadata transaction."""

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

    try:
        with transaction.atomic():
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
        delete_storage_key(stored.storage_key)
        raise
    return asset


def delete_image(user, asset_type: str) -> None:
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
        profile = (
            UserProfile.objects.select_for_update()
            .filter(user=user)
            .first()
        )
        if profile is None:
            return
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
