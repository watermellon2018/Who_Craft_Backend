"""Reference-aware cleanup hooks for hard deletes and replaced media."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterImage,
    CharacterVariant,
)
from w_craft_back.characters.creating.models import Character
from w_craft_back.movie.poster.models import PosterVariant
from w_craft_back.movie.music.models import MusicAsset
from w_craft_back.movie.project.dashboard_models import (
    Location,
    MusicTrack,
    ProjectAsset,
    Scene,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.profile.models import UserAsset, UserProfile
from w_craft_back.storage_gateway import (
    StorageGatewayError,
    delete_storage_key,
    safe_storage_key,
    storage_key_from_legacy_url,
)


logger = logging.getLogger(__name__)

_FILE_FIELDS = {
    Project: ("image", "cover_image"),
    ProjectAsset: ("file",),
    Location: ("image",),
    Scene: ("preview_image",),
    MusicTrack: ("audio_file", "cover_image"),
    MusicAsset: ("file",),
    PosterVariant: ("image", "thumbnail"),
    UserProfile: ("avatar", "cover"),
    Character: ("photo",),
}
_CUSTOM_SENDERS = {
    CharacterAsset,
    CharacterImage,
    CharacterVariant,
    UserAsset,
}


def _safe_key(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return safe_storage_key(str(value))
    except StorageGatewayError:
        logger.warning("Ignoring unsafe legacy media key")
        return None


def _legacy_url_candidates(key: str) -> tuple[str, ...]:
    normalized = safe_storage_key(key)
    return (normalized, f"/media/{normalized}")


def _field_storage_keys(instance) -> set[str]:
    keys: set[str] = set()
    for field_name in _FILE_FIELDS.get(type(instance), ()):
        field = getattr(instance, field_name, None)
        name = getattr(field, "name", "") if field else ""
        if name:
            key = _safe_key(str(name))
            if key:
                keys.add(key)
    if isinstance(instance, (CharacterAsset, CharacterImage)):
        storage_path = getattr(instance, "storage_path", "")
        legacy_url = getattr(instance, "image_url", "")
        key = storage_path or storage_key_from_legacy_url(legacy_url)
        if key:
            normalized = _safe_key(str(key))
            if normalized:
                keys.add(normalized)
    if isinstance(instance, CharacterVariant):
        key = storage_key_from_legacy_url(instance.image_url)
        if key:
            normalized = _safe_key(str(key))
            if normalized:
                keys.add(normalized)
    if isinstance(instance, UserAsset) and instance.storage_key:
        key = _safe_key(instance.storage_key)
        if key:
            keys.add(key)
    return keys


def _storage_key_is_referenced(storage_key: str) -> bool:
    key = safe_storage_key(storage_key)
    for model, field_names in _FILE_FIELDS.items():
        for field_name in field_names:
            if model.objects.filter(**{field_name: key}).exists():
                return True

    candidates = _legacy_url_candidates(key)
    if CharacterAsset.objects.filter(
        Q(storage_path=key)
        | Q(image_url__in=candidates)
        | Q(image_url__endswith=f"/media/{key}")
    ).exists():
        return True
    if CharacterImage.objects.filter(
        Q(storage_path=key)
        | Q(image_url__in=candidates)
        | Q(image_url__endswith=f"/media/{key}")
    ).exists():
        return True
    if CharacterVariant.objects.filter(
        Q(image_url__in=candidates) | Q(image_url__endswith=f"/media/{key}")
    ).exists():
        return True
    try:
        retention_hours = max(
            int(getattr(settings, "MEDIA_ORPHAN_RETENTION_HOURS", 24)),
            1,
        )
    except (TypeError, ValueError):
        retention_hours = 24
    retention_cutoff = timezone.now() - timedelta(hours=retention_hours)
    return UserAsset.objects.filter(storage_key=key).filter(
        Q(deleted_at__isnull=True) | Q(deleted_at__gte=retention_cutoff)
    ).exists()


def _delete_after_commit(storage_keys: set[str]) -> None:
    if not storage_keys:
        return

    def cleanup() -> None:
        for key in storage_keys:
            try:
                if not _storage_key_is_referenced(key):
                    delete_storage_key(key)
            except (OSError, StorageGatewayError):
                logger.exception("Failed to delete media object key=%s", key)

    transaction.on_commit(cleanup)


@receiver(pre_save)
def capture_replaced_media(sender, instance, **kwargs) -> None:
    """Remember old keys before a successful model replacement."""

    if sender not in _FILE_FIELDS and sender not in _CUSTOM_SENDERS:
        return
    if not instance.pk:
        return
    previous = sender.objects.filter(pk=instance.pk).first()
    if previous is None:
        return
    old_keys = _field_storage_keys(previous)
    new_keys = _field_storage_keys(instance)
    instance._storage_gateway_replaced_keys = old_keys - new_keys


@receiver(post_save)
def delete_replaced_media(sender, instance, **kwargs) -> None:
    """Delete replaced files only after the database transaction commits."""

    if sender not in _FILE_FIELDS and sender not in _CUSTOM_SENDERS:
        return
    keys = getattr(instance, "_storage_gateway_replaced_keys", set())
    _delete_after_commit(set(keys))


@receiver(post_delete)
def delete_model_media(sender, instance, **kwargs) -> None:
    """Delete a binary only after its final aggregate reference disappears."""

    if sender not in _FILE_FIELDS and sender not in _CUSTOM_SENDERS:
        return
    _delete_after_commit(_field_storage_keys(instance))
