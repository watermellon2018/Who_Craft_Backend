"""Idempotently probe legacy music objects after the additive schema deploy."""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand

from w_craft_back.movie.music.models import (
    MusicAsset,
    MusicAssetOrigin,
    MusicAssetVerificationStatus,
)
from w_craft_back.storage_gateway import StorageGatewayError, probe_stored_audio


class Command(BaseCommand):
    help = "Probe legacy music files and fill verified metadata without migration I/O."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=1000)

    def handle(self, *args, **options):
        limit = max(1, min(int(options["limit"]), 10000))
        assets = MusicAsset.objects.filter(
            origin=MusicAssetOrigin.LEGACY,
            verification_status__in=(
                MusicAssetVerificationStatus.LEGACY_UNVERIFIED,
                MusicAssetVerificationStatus.MISSING,
            ),
        ).order_by("created_at")[:limit]
        verified = 0
        missing = 0
        invalid = 0
        for asset in assets:
            key = getattr(asset.file, "name", "")
            try:
                audio = probe_stored_audio(key)
            except (FileNotFoundError, OSError):
                MusicAsset.objects.filter(pk=asset.pk).update(
                    verification_status=MusicAssetVerificationStatus.MISSING
                )
                missing += 1
                continue
            except StorageGatewayError:
                invalid += 1
                continue
            MusicAsset.objects.filter(pk=asset.pk).update(
                mime_type=audio.mime_type,
                size_bytes=len(audio.data),
                checksum_sha256=audio.sha256,
                duration_seconds=Decimal(str(audio.duration_seconds)).quantize(
                    Decimal("0.001")
                ),
                verification_status=MusicAssetVerificationStatus.VERIFIED,
            )
            verified += 1
        self.stdout.write(
            self.style.SUCCESS(
                "Legacy music probe complete: "
                f"verified={verified}, missing={missing}, invalid={invalid}."
            )
        )
