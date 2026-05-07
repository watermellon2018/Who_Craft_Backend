from __future__ import annotations

from typing import Iterable

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    Interest,
    UserAsset,
    UserInterest,
    UserProfile,
    UserSocialLink,
)


AVATAR_MAX_BYTES = 5 * 1024 * 1024
COVER_MAX_BYTES = 10 * 1024 * 1024


class FileTooLarge(Exception):
    pass


class UnsupportedMediaType(Exception):
    pass


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


def _validate_image_upload(django_file, max_bytes: int) -> None:
    content_type = getattr(django_file, 'content_type', '') or ''
    if not content_type.lower().startswith('image/'):
        raise UnsupportedMediaType(content_type or 'unknown')
    size = getattr(django_file, 'size', 0) or 0
    if size > max_bytes:
        raise FileTooLarge(f'{size} > {max_bytes}')


def _read_image_dimensions(django_file) -> tuple[int | None, int | None]:
    try:
        from PIL import Image  # provided by Pillow (Django ImageField dep)
        django_file.seek(0)
        with Image.open(django_file) as img:
            return img.width, img.height
    except Exception:
        return None, None
    finally:
        try:
            django_file.seek(0)
        except Exception:
            pass


@transaction.atomic
def save_uploaded_image(user, django_file, asset_type: str) -> UserAsset:
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

    _validate_image_upload(django_file, max_bytes)
    width, height = _read_image_dimensions(django_file)

    profile, _ = UserProfile.objects.get_or_create(user=user)
    setattr(profile, field_name, django_file)
    profile.save()

    image_field = getattr(profile, field_name)
    storage_key = image_field.name
    asset = UserAsset.objects.create(
        user=user,
        type=asset_type,
        storage_key=storage_key,
        url=None,
        mime_type=getattr(django_file, 'content_type', None),
        size_bytes=getattr(django_file, 'size', None),
        width=width,
        height=height,
    )

    UserAsset.objects.filter(
        user=user, type=asset_type, deleted_at__isnull=True
    ).exclude(pk=asset.pk).update(deleted_at=timezone.now())

    setattr(profile, link_field, asset)
    profile.save(update_fields=[link_field])

    return asset


@transaction.atomic
def delete_image(user, asset_type: str) -> None:
    if asset_type == UserAsset.AVATAR:
        field_name = 'avatar'
        link_field = 'avatar_asset'
    elif asset_type == UserAsset.COVER:
        field_name = 'cover'
        link_field = 'cover_asset'
    else:
        raise ValueError(f'unsupported asset_type: {asset_type}')

    profile = UserProfile.objects.filter(user=user).first()
    if profile is None:
        return

    image_field = getattr(profile, field_name)
    if image_field:
        image_field.delete(save=False)
    setattr(profile, field_name, None)
    setattr(profile, link_field, None)
    profile.save(update_fields=[field_name, link_field])

    UserAsset.objects.filter(
        user=user, type=asset_type, deleted_at__isnull=True
    ).update(deleted_at=timezone.now())
