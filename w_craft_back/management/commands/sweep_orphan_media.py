"""Delete unreferenced managed media after a configurable retention window."""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterImage,
    CharacterVariant,
)
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
    safe_storage_key,
    storage_key_from_legacy_url,
)


MANAGED_PREFIXES = (
    "avatars",
    "covers",
    "profiles",
    "project",
    "projects",
    "character-studio",
    "mock",
    "locations",
    "scenes",
)


def _safe_key(value) -> str | None:
    if not value:
        return None
    try:
        return safe_storage_key(value)
    except StorageGatewayError:
        return None


def _file_names(queryset, *field_names: str) -> set[str]:
    result: set[str] = set()
    for row in queryset.values_list(*field_names):
        for value in row:
            key = _safe_key(value)
            if key:
                result.add(key)
    return result


def referenced_storage_keys(cutoff) -> set[str]:
    """Return live and retention-protected storage keys."""

    keys = _file_names(Project.objects.all(), "cover_image")
    keys |= _file_names(ProjectAsset.objects.all(), "file")
    keys |= _file_names(Location.objects.all(), "image")
    keys |= _file_names(Scene.objects.all(), "preview_image")
    keys |= _file_names(MusicTrack.objects.all(), "audio_file", "cover_image")
    keys |= _file_names(MusicAsset.objects.all(), "file")
    keys |= _file_names(UserProfile.objects.all(), "avatar", "cover")
    for model in (CharacterAsset, CharacterImage):
        for value in model.objects.exclude(storage_path="").values_list(
            "storage_path",
            flat=True,
        ):
            key = _safe_key(value)
            if key:
                keys.add(key)
    for model in (CharacterAsset, CharacterImage, CharacterVariant):
        for raw_url in model.objects.exclude(image_url="").values_list(
            "image_url",
            flat=True,
        ):
            legacy_key = storage_key_from_legacy_url(raw_url)
            if legacy_key:
                keys.add(legacy_key)
    retained_variants = PosterVariant.objects.filter(
        Q(is_deleted=False) | Q(updated_at__gte=cutoff)
    )
    keys |= _file_names(retained_variants, "image", "thumbnail")
    retained_user_assets = UserAsset.objects.filter(
        Q(deleted_at__isnull=True) | Q(deleted_at__gte=cutoff)
    )
    for value in retained_user_assets.exclude(storage_key="").values_list(
        "storage_key",
        flat=True,
    ):
        key = _safe_key(value)
        if key:
            keys.add(key)
    return keys


def _walk_storage(prefix: str) -> Iterable[str]:
    directories = [safe_storage_key(prefix)]
    while directories:
        current = directories.pop()
        try:
            child_directories, files = default_storage.listdir(current)
        except (FileNotFoundError, NotImplementedError, OSError):
            continue
        directories.extend(
            safe_storage_key(f"{current}/{name}") for name in child_directories
        )
        for filename in files:
            yield safe_storage_key(f"{current}/{filename}")


class Command(BaseCommand):
    help = "Delete unreferenced managed media older than the retention window."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--retention-hours",
            type=int,
            default=int(getattr(settings, "MEDIA_ORPHAN_RETENTION_HOURS", 168)),
        )
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument("--delete", action="store_true")

    def handle(self, *args, **options):
        retention_hours = max(1, int(options["retention_hours"]))
        limit = max(1, int(options["limit"]))
        cutoff = timezone.now() - timedelta(hours=retention_hours)
        referenced = referenced_storage_keys(cutoff)
        deleted = 0
        candidates = 0

        for prefix in MANAGED_PREFIXES:
            for key in _walk_storage(prefix):
                if key in referenced:
                    continue
                try:
                    modified_at = default_storage.get_modified_time(key)
                except (FileNotFoundError, NotImplementedError, OSError):
                    continue
                if modified_at > cutoff:
                    continue
                candidates += 1
                if options["delete"]:
                    default_storage.delete(key)
                    deleted += 1
                if candidates >= limit:
                    break
            if candidates >= limit:
                break

        action = "deleted" if options["delete"] else "would delete"
        affected = deleted if options["delete"] else candidates
        self.stdout.write(
            self.style.SUCCESS(
                f"Media sweep complete: {action} {affected} object(s), "
                f"retention={retention_hours}h."
            )
        )
