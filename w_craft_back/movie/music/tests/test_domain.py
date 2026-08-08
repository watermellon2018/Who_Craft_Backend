from __future__ import annotations

from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from w_craft_back.movie.music.models import (
    MusicAsset,
    MusicAssetOrigin,
    MusicAssetRole,
    MusicAssetVerificationStatus,
    MusicTrackVersion,
)
from w_craft_back.movie.project.dashboard_models import MusicTrack, Scene, SceneMusic

from .helpers import make_project, make_user


class MusicDomainTests(TestCase):
    def setUp(self) -> None:
        self.owner = make_user("domain-owner")
        self.project = make_project(self.owner)
        self.other = make_project(make_user("domain-other"), "Other")

    def asset(self, project=None) -> MusicAsset:
        return MusicAsset.objects.create(
            project=project or self.project,
            file="projects/music/test.wav",
            asset_role=MusicAssetRole.GENERATED,
            origin=MusicAssetOrigin.GENERATED,
            mime_type="audio/wav",
            size_bytes=44,
            checksum_sha256="a" * 64,
            duration_seconds=1,
            verification_status=MusicAssetVerificationStatus.VERIFIED,
            created_by=self.owner,
        )

    def test_track_version_is_cross_project_safe_and_immutable(self):
        track = MusicTrack.objects.create(project=self.project, title="Track")
        other_asset = self.asset(self.other)
        with self.assertRaises(ValidationError):
            MusicTrackVersion.objects.create(
                track=track,
                version_number=1,
                asset=other_asset,
            )
        version = MusicTrackVersion.objects.create(
            track=track,
            version_number=1,
            asset=self.asset(),
        )
        version.version_number = 2
        with self.assertRaises(ValidationError):
            version.save()
        with self.assertRaises(IntegrityError):
            MusicTrackVersion.objects.create(
                track=track,
                version_number=1,
                asset=self.asset(),
            )

    def test_scene_music_requires_a_version_of_the_same_track(self):
        track = MusicTrack.objects.create(project=self.project, title="Track")
        other_track = MusicTrack.objects.create(project=self.project, title="Other")
        version = MusicTrackVersion.objects.create(
            track=other_track,
            version_number=1,
            asset=self.asset(),
        )
        scene = Scene.objects.create(project=self.project, title="Scene", order=1)
        with self.assertRaises(ValidationError):
            SceneMusic.objects.create(
                scene=scene,
                track=track,
                track_version=version,
            )

    def test_legacy_backfill_never_reads_storage_and_keeps_zero_duration_null(self):
        track = MusicTrack.objects.create(
            project=self.project,
            title="Legacy",
            audio_file="projects/music/missing.wav",
            duration_seconds=0,
            created_by=self.owner,
        )
        migration = import_module(
            "w_craft_back.migrations."
            "0049_musictrack_archived_at_musictrack_source_musicasset_and_more"
        )
        with patch("django.core.files.storage.default_storage.open") as storage_open:
            migration.backfill_legacy_music_tracks(apps, None)
        storage_open.assert_not_called()
        track.refresh_from_db()
        self.assertEqual(track.source, "legacy")
        self.assertIsNotNone(track.active_version_id)
        self.assertIsNone(track.active_version.asset.duration_seconds)
        self.assertEqual(
            track.active_version.asset.verification_status,
            MusicAssetVerificationStatus.LEGACY_UNVERIFIED,
        )
        self.assertEqual(track.audio_file.name, "projects/music/missing.wav")

    def test_legacy_backfill_skips_null_and_empty_audio_file(self):
        null_track = MusicTrack.objects.create(
            project=self.project,
            title="Null audio",
            audio_file=None,
            created_by=self.owner,
        )
        empty_track = MusicTrack.objects.create(
            project=self.project,
            title="Empty audio",
            audio_file="",
            created_by=self.owner,
        )
        migration = import_module(
            "w_craft_back.migrations."
            "0049_musictrack_archived_at_musictrack_source_musicasset_and_more"
        )

        migration.backfill_legacy_music_tracks(apps, None)

        null_track.refresh_from_db()
        empty_track.refresh_from_db()
        self.assertIsNone(null_track.active_version_id)
        self.assertIsNone(empty_track.active_version_id)
        self.assertFalse(
            MusicAsset.objects.filter(
                project=self.project,
                origin=MusicAssetOrigin.LEGACY,
            ).exists()
        )
