from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
        track = SimpleNamespace(
            project_id=self.project.pk,
            audio_file="projects/music/missing.wav",
            duration_seconds=0,
            created_by_id=self.owner.pk,
            pk=17,
        )
        historical_track = MagicMock()
        filtered_tracks = historical_track.objects.filter.return_value
        filtered_tracks.exclude.return_value.iterator.return_value = [track]
        historical_asset = MagicMock()
        historical_asset.objects.create.return_value = SimpleNamespace(pk="asset-1")
        historical_version = MagicMock()
        historical_version.objects.create.return_value = SimpleNamespace(pk="version-1")
        historical_scene_music = MagicMock()
        historical_models = {
            "MusicTrack": historical_track,
            "MusicAsset": historical_asset,
            "MusicTrackVersion": historical_version,
            "SceneMusic": historical_scene_music,
        }
        historical_apps = MagicMock()
        historical_apps.get_model.side_effect = (
            lambda _app_label, model_name: historical_models[model_name]
        )
        migration = import_module(
            "w_craft_back.migrations."
            "0049_musictrack_archived_at_musictrack_source_musicasset_and_more"
        )
        with patch("django.core.files.storage.default_storage.open") as storage_open:
            migration.backfill_legacy_music_tracks(historical_apps, None)
        storage_open.assert_not_called()
        historical_asset.objects.create.assert_called_once_with(
            project_id=self.project.pk,
            file="projects/music/missing.wav",
            asset_role="generated",
            origin="legacy",
            duration_seconds=None,
            verification_status="legacy_unverified",
            moderation_status="not_required",
            created_by_id=self.owner.pk,
        )
        historical_track.objects.filter.assert_any_call(pk=17)
        historical_track.objects.filter.return_value.update.assert_called_once_with(
            active_version_id="version-1",
            source="legacy",
        )

    def test_legacy_backfill_skips_null_and_empty_audio_file(self):
        historical_track = MagicMock()
        filtered_tracks = historical_track.objects.filter.return_value
        filtered_tracks.exclude.return_value.iterator.return_value = []
        historical_asset = MagicMock()
        historical_models = {
            "MusicTrack": historical_track,
            "MusicAsset": historical_asset,
            "MusicTrackVersion": MagicMock(),
            "SceneMusic": MagicMock(),
        }
        historical_apps = MagicMock()
        historical_apps.get_model.side_effect = (
            lambda _app_label, model_name: historical_models[model_name]
        )
        migration = import_module(
            "w_craft_back.migrations."
            "0049_musictrack_archived_at_musictrack_source_musicasset_and_more"
        )

        migration.backfill_legacy_music_tracks(historical_apps, None)

        historical_asset.objects.create.assert_not_called()
