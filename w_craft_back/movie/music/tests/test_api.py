"""Integration tests for the public, project-scoped Music Studio API."""

from __future__ import annotations

import hashlib
import io
import tempfile
import wave
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.music.models import (
    MusicAsset,
    MusicAssetOrigin,
    MusicAssetRole,
    MusicAssetVerificationStatus,
    MusicGenerationJob,
    MusicJobStage,
    MusicJobStatus,
    MusicModerationStatus,
    MusicTrackVersion,
    MusicVariant,
    MusicVariantStatus,
)
from w_craft_back.movie.music.serializers import MusicBriefSerializer
from w_craft_back.movie.project.dashboard_models import (
    Location,
    MusicTrack,
    ProjectMember,
    ProjectMemberRole,
    Scene,
    SceneCharacter,
    SceneMusic,
)
from w_craft_back.movie.project.models import Project


def _user(username: str) -> tuple[User, UserKey]:
    user = User.objects.create_user(username=username, password="pw")
    return user, UserKey.objects.create(user=user)


def _project(owner: User, key: UserKey, title: str) -> Project:
    project = Project.objects.create(
        owner=owner,
        user=key,
        title=title,
        format="full-movie",
        annot="",
        desc="",
    )
    ProjectMember.objects.create(
        project=project,
        user=owner,
        role=ProjectMemberRole.OWNER,
    )
    return project


def _brief(*, mode: str = "instrumental", title: str = "Night passage") -> dict:
    content: dict = {"mode": mode}
    purpose = "underscore"
    if mode == "song":
        purpose = "song"
        content.update(
            {
                "lyricsLanguage": "ru",
                "vocalStyle": {
                    "timbre": "warm",
                    "delivery": "intimate",
                    "density": "balanced",
                },
                "sections": [
                    {
                        "type": "verse",
                        "label": "Куплет 1",
                        "text": "Ночь оставляет свет в окне\nИ город слушает шаги",
                    },
                    {
                        "type": "chorus",
                        "label": "Припев",
                        "text": "Мы всё равно найдём дорогу",
                    },
                ],
            }
        )
    return {
        "context": {"type": "project"},
        "content": content,
        "title": title,
        "purpose": purpose,
        "genre": "cinematic_pop" if mode == "song" else "cinematic",
        "moods": ["tense", "hopeful"],
        "durationSeconds": 12,
        "tempo": {"mode": "bpm", "bpm": 92},
        "energyCurve": "build",
        "instruments": ["low_strings", "analog_pulse"],
        "exclude": ["bright_brass"],
        "loopable": False,
        "textRefinement": "Leave an unresolved ending.",
    }


def _wav_upload(name: str = "reference.wav", duration: float = 0.1):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\0\0" * max(1, round(duration * 8000)))
    return SimpleUploadedFile(name, output.getvalue(), content_type="audio/wav")


class MusicBriefSerializerTests(TestCase):
    def test_instrumental_rejects_lyrics(self) -> None:
        brief = _brief()
        brief["content"] = {
            "mode": "instrumental",
            "lyricsLanguage": "ru",
            "sections": [{"type": "verse", "text": "should not pass"}],
        }

        serializer = MusicBriefSerializer(data=brief)

        self.assertFalse(serializer.is_valid())
        self.assertIn("content", serializer.errors)

    def test_song_preserves_order_line_breaks_and_full_sound_brief(self) -> None:
        brief = _brief(mode="song")

        serializer = MusicBriefSerializer(data=brief)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        normalized = serializer.validated_data
        self.assertEqual(
            [section["type"] for section in normalized["content"]["sections"]],
            ["verse", "chorus"],
        )
        self.assertIn("\n", normalized["content"]["sections"][0]["text"])
        for field in (
            "genre",
            "durationSeconds",
            "moods",
            "instruments",
            "energyCurve",
            "tempo",
            "textRefinement",
        ):
            self.assertEqual(normalized[field], brief[field])


@override_settings(
    SIGNED_MEDIA_TTL_SECONDS=120,
    MUSIC_MIN_REFERENCE_DURATION_SECONDS=0.01,
    MUSIC_MAX_REFERENCE_DURATION_SECONDS=300,
)
class MusicApiTests(TestCase):
    def setUp(self) -> None:
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.client = APIClient()
        self.owner, self.owner_key = _user("music-owner")
        self.viewer, self.viewer_key = _user("music-viewer")
        self.outsider, self.outsider_key = _user("music-outsider")
        self.project = _project(self.owner, self.owner_key, "Music film")
        ProjectMember.objects.create(
            project=self.project,
            user=self.viewer,
            role=ProjectMemberRole.VIEWER,
        )
        self.other_project = _project(
            self.outsider,
            self.outsider_key,
            "Other film",
        )

    @property
    def root(self) -> str:
        return f"/api/projects/{self.project.id}/music/"

    def _header(self, key: UserKey | None = None) -> dict[str, str]:
        return {"HTTP_X_USER_TOKEN": str((key or self.owner_key).key)}

    def _asset(
        self,
        *,
        project: Project | None = None,
        role: str = MusicAssetRole.GENERATED,
        origin: str = MusicAssetOrigin.GENERATED,
        name: str = "generated.wav",
    ) -> MusicAsset:
        target_project = project or self.project
        payload = b"RIFFtest-audio"
        asset = MusicAsset(
            project=target_project,
            asset_role=role,
            origin=origin,
            original_name=name,
            mime_type="audio/wav",
            size_bytes=len(payload),
            checksum_sha256=hashlib.sha256(payload).hexdigest(),
            duration_seconds=Decimal("12.000"),
            verification_status=MusicAssetVerificationStatus.VERIFIED,
            moderation_status=(
                MusicModerationStatus.PENDING
                if role == MusicAssetRole.REFERENCE
                else MusicModerationStatus.NOT_REQUIRED
            ),
            created_by=self.owner,
        )
        if role == MusicAssetRole.REFERENCE:
            asset.rights_confirmed_by = self.owner
            asset.rights_confirmed_at = timezone.now()
            asset.rights_statement_version = "music-reference-v1"
        asset.file.name = f"tests/music/{target_project.id}/{name}"
        asset.save()
        return asset

    def _track_with_version(
        self,
        *,
        title: str = "Library track",
        project: Project | None = None,
    ) -> tuple[MusicTrack, MusicTrackVersion]:
        target_project = project or self.project
        track = MusicTrack.objects.create(
            project=target_project,
            title=title,
            author="Craft AI",
            tags=["cinematic"],
            source="generated",
            created_by=target_project.owner,
            updated_by=target_project.owner,
        )
        version = MusicTrackVersion.objects.create(
            track=track,
            version_number=1,
            asset=self._asset(project=target_project, name=f"{track.id}.wav"),
            brief_snapshot=_brief(),
            created_by=target_project.owner,
        )
        track.active_version = version
        track.save(update_fields=("active_version", "updated_at"))
        return track, version

    def _completed_job(self) -> tuple[MusicGenerationJob, MusicVariant]:
        job = MusicGenerationJob.objects.create(
            project=self.project,
            actor=self.owner,
            brief=_brief(),
            compiled_request={"contentMode": "instrumental"},
            provider="mock",
            model_name="deterministic-wav-v1",
            status=MusicJobStatus.COMPLETED,
            stage=MusicJobStage.FINALIZED,
            variant_count=1,
            idempotency_key="completed-job",
            request_fingerprint="a" * 64,
            completed_at=timezone.now(),
        )
        variant = MusicVariant.objects.create(
            job=job,
            asset=self._asset(name="variant.wav"),
            variant_index=0,
            seed=123,
            status=MusicVariantStatus.GENERATED,
        )
        return job, variant

    def test_every_music_view_requires_header_token(self) -> None:
        legacy_body = self.client.post(
            self.root,
            {
                "token_user": str(self.owner_key.key),
                "title": "Body token must not work",
            },
            format="json",
        )
        query_token = self.client.get(
            self.root,
            {"token_user": str(self.owner_key.key)},
        )

        self.assertEqual(legacy_body.status_code, 401)
        self.assertEqual(query_token.status_code, 401)

    def test_legacy_metadata_post_keeps_payload_and_response(self) -> None:
        response = self.client.post(
            self.root,
            {
                "title": "Metadata only",
                "author": "Composer",
                "duration_seconds": 0,
                "tags": ["draft"],
            },
            format="json",
            **self._header(),
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.json()), {"id", "title"})
        self.assertEqual(response.json()["title"], "Metadata only")

    def test_track_patch_accepts_legacy_shape_and_preserves_optional_lock(self) -> None:
        track, _version = self._track_with_version()
        url = f"{self.root}{track.id}/"

        legacy = self.client.patch(url, {"title": "Legacy editor", "duration_seconds": 48}, format="json", **self._header())
        self.assertEqual(legacy.status_code, 200, legacy.json())
        self.assertEqual(legacy.json()["version"], 2)
        self.assertEqual(legacy.json()["activeVersion"]["durationSeconds"], 12.0)
        track.refresh_from_db()
        self.assertEqual(track.duration_seconds, 48)

        stale = self.client.patch(url, {"version": 1, "durationSeconds": 49}, format="json", **self._header())
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["currentVersion"], 2)

        current = self.client.patch(url, {"version": 2, "durationSeconds": 49}, format="json", **self._header())
        self.assertEqual(current.status_code, 200, current.json())
        self.assertEqual(current.json()["version"], 3)
        track.refresh_from_db()
        self.assertEqual(track.duration_seconds, 49)

        no_op = self.client.patch(url, {"version": 3}, format="json", **self._header())
        self.assertEqual(no_op.status_code, 200, no_op.json())
        self.assertEqual(no_op.json()["version"], 3)

        conflicting_aliases = self.client.patch(url, {"durationSeconds": 50, "duration_seconds": 51}, format="json", **self._header())
        self.assertEqual(conflicting_aliases.status_code, 400)

    def test_capabilities_and_enqueue_honor_bounded_seed(self) -> None:
        capabilities = self.client.get(f"{self.root}capabilities/", **self._header())
        self.assertEqual(capabilities.status_code, 200)
        self.assertTrue(capabilities.json()["supportsSeed"])

        brief = _brief()
        brief["seed"] = 123456
        accepted = self.client.post(
            f"{self.root}generation-jobs/",
            {"variantCount": 2, "brief": brief},
            format="json",
            HTTP_IDEMPOTENCY_KEY="explicit-seed",
            **self._header(),
        )
        self.assertEqual(accepted.status_code, 202, accepted.json())
        job = MusicGenerationJob.objects.get(pk=accepted.json()["jobId"])
        self.assertEqual(job.brief["seed"], 123456)
        self.assertEqual(job.compiled_request["baseSeed"], 123456)

        invalid_brief = _brief()
        invalid_brief["seed"] = 4_294_967_296
        invalid = self.client.post(
            f"{self.root}generation-jobs/",
            {"variantCount": 1, "brief": invalid_brief},
            format="json",
            HTTP_IDEMPOTENCY_KEY="invalid-seed",
            **self._header(),
        )
        self.assertEqual(invalid.status_code, 400)

    def test_library_search_filter_signed_expiry_and_permissions(self) -> None:
        active, version = self._track_with_version(title="Night Station")
        archived, _ = self._track_with_version(title="Old Daylight")
        archived.archived_at = timezone.now()
        archived.save(update_fields=("archived_at", "updated_at"))
        scene = Scene.objects.create(project=self.project, title="Scene", order=1)
        SceneMusic.objects.create(
            scene=scene,
            track=active,
            track_version=version,
        )

        response = self.client.get(
            self.root,
            {"q": "station", "status": "active"},
            **self._header(self.viewer_key),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["items"]], [active.id])
        self.assertEqual(payload["items"][0]["usageCount"], 1)
        self.assertTrue(payload["items"][0]["activeVersion"]["audioUrl"])
        self.assertTrue(
            payload["items"][0]["activeVersion"]["audioUrlExpiresAt"]
        )
        self.assertFalse(payload["permissions"]["canEdit"])
        self.assertFalse(payload["permissions"]["canRunGeneration"])

    def test_track_provenance_cannot_override_canonical_fields(self) -> None:
        track, version = self._track_with_version()
        asset = version.asset
        asset.provider = "mock"
        asset.model_name = "deterministic-wav-v1"
        asset.provider_request_id = "request-123"
        asset.provenance = {
            "provider": "spoofed",
            "model": "spoofed",
            "createdByAi": False,
            "watermark": True,
            "internalSecret": "must-not-leak",
        }
        asset.save(update_fields=("provider", "model_name", "provider_request_id", "provenance", "updated_at"))

        response = self.client.get(f"{self.root}{track.id}/", **self._header(self.viewer_key))
        self.assertEqual(response.status_code, 200)
        provenance = response.json()["activeVersion"]["provenance"]
        self.assertTrue(provenance["createdByAi"])
        self.assertEqual(provenance["provider"], "mock")
        self.assertEqual(provenance["model"], "deterministic-wav-v1")
        self.assertTrue(provenance["watermark"])
        self.assertNotIn("internalSecret", provenance)

    def test_scene_options_search_number_location_summary_character_and_act(self) -> None:
        location = Location.objects.create(project=self.project, name="Старая станция")
        scene = Scene.objects.create(
            project=self.project,
            location=location,
            title="Ночной переход",
            order=7,
            act=2,
            mood="tense",
            duration_seconds=80,
            description="Герои уходят до прибытия патруля. " * 30,
        )
        character = StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Лея",
        )
        SceneCharacter.objects.create(scene=scene, character=character)

        for query in ("7", "переход", "станция", "патруля", "Лея"):
            response = self.client.get(
                f"{self.root}scene-options/",
                {"q": query, "act": 2},
                **self._header(),
            )
            self.assertEqual(response.status_code, 200, query)
            self.assertEqual(response.json()["items"][0]["sceneId"], scene.id)
        item = response.json()["items"][0]
        self.assertLessEqual(len(item["summary"]), 240)
        self.assertEqual(item["characters"], ["Лея"])

    def test_scene_options_scene_id_returns_exact_scene_without_leak(self) -> None:
        for order in range(1, 4):
            Scene.objects.create(
                project=self.project,
                title=f"First page {order}",
                order=order,
            )
        exact = Scene.objects.create(
            project=self.project,
            title="Deep-linked scene",
            order=99,
            act=2,
        )
        foreign = Scene.objects.create(
            project=self.other_project,
            title="Foreign scene",
            order=100,
        )

        response = self.client.get(
            f"{self.root}scene-options/",
            {"sceneId": exact.id, "limit": 1, "q": "no match", "act": 3},
            **self._header(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["sceneId"] for item in response.json()["items"]],
            [exact.id],
        )
        for scene_id in (foreign.id, foreign.id + 1000):
            with self.subTest(scene_id=scene_id):
                missing = self.client.get(
                    f"{self.root}scene-options/",
                    {"sceneId": scene_id, "limit": 1},
                    **self._header(),
                )
                self.assertEqual(missing.status_code, 200)
                self.assertEqual(missing.json()["items"], [])

    def test_reference_upload_rights_delete_and_cross_project_isolation(self) -> None:
        denied = self.client.post(
            f"{self.root}reference-assets/",
            {
                "file": _wav_upload(),
                "rightsConfirmed": "false",
                "rightsStatementVersion": "music-reference-v1",
            },
            format="multipart",
            **self._header(),
        )
        self.assertEqual(denied.status_code, 400)

        accepted = self.client.post(
            f"{self.root}reference-assets/",
            {
                "file": _wav_upload(),
                "rightsConfirmed": "true",
                "rightsStatementVersion": "music-reference-v1",
            },
            format="multipart",
            **self._header(),
        )
        self.assertEqual(accepted.status_code, 201, accepted.json())
        payload = accepted.json()
        self.assertEqual(payload["localVerificationStatus"], "accepted")
        self.assertTrue(payload["audioUrlExpiresAt"])

        foreign = self._asset(
            project=self.other_project,
            role=MusicAssetRole.REFERENCE,
            origin=MusicAssetOrigin.UPLOAD,
            name="foreign.wav",
        )
        cross = self.client.post(
            f"{self.root}generation-jobs/",
            {
                "referenceAssetId": str(foreign.id),
                "variantCount": 1,
                "brief": _brief(),
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="cross-reference",
            **self._header(),
        )
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(cross.json()["code"], "MUSIC_REFERENCE_NOT_FOUND")

        deleted = self.client.delete(
            f"{self.root}reference-assets/{payload['assetId']}/",
            **self._header(),
        )
        self.assertEqual(deleted.status_code, 204)

    def test_enqueue_refresh_song_and_idempotency(self) -> None:
        body = {"variantCount": 2, "brief": _brief(mode="song")}
        first = self.client.post(
            f"{self.root}generation-jobs/",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY="song-enqueue",
            **self._header(),
        )
        replay = self.client.post(
            f"{self.root}generation-jobs/",
            body,
            format="json",
            HTTP_IDEMPOTENCY_KEY="song-enqueue",
            **self._header(),
        )

        self.assertEqual(first.status_code, 202, first.json())
        self.assertEqual(replay.status_code, 202, replay.json())
        self.assertEqual(first.json()["jobId"], replay.json()["jobId"])
        self.assertFalse(first.json()["idempotentReplay"])
        self.assertTrue(replay.json()["idempotentReplay"])

        detail = self.client.get(
            f"{self.root}generation-jobs/{first.json()['jobId']}/",
            **self._header(),
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["brief"], body["brief"])
        self.assertIn("permissions", detail.json())

        changed = {"variantCount": 2, "brief": _brief(mode="song", title="Changed")}
        conflict = self.client.post(
            f"{self.root}generation-jobs/",
            changed,
            format="json",
            HTTP_IDEMPOTENCY_KEY="song-enqueue",
            **self._header(),
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "MUSIC_IDEMPOTENCY_CONFLICT")

    def test_apply_is_idempotent_and_viewer_cannot_mutate(self) -> None:
        original_target, _ = self._track_with_version(title="Original target")
        job, variant = self._completed_job()
        job.target_track = original_target
        job.save(update_fields=("target_track", "updated_at"))
        url = f"{self.root}generation-jobs/{job.id}/variants/{variant.id}/apply/"
        body = {
            "targetTrackId": None,
            "expectedTrackVersion": None,
            "title": "Applied result",
            "author": "Craft AI",
            "tags": ["cinematic"],
            "makeActive": True,
        }

        denied = self.client.post(
            url,
            body,
            format="json",
            **self._header(self.viewer_key),
        )
        first = self.client.post(url, body, format="json", **self._header())
        replay = self.client.post(url, body, format="json", **self._header())

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(first.status_code, 201, first.json())
        self.assertEqual(replay.status_code, 200, replay.json())
        self.assertNotEqual(first.json()["trackId"], original_target.id)
        self.assertTrue(replay.json()["idempotentReplay"])
        self.assertEqual(
            MusicTrackVersion.objects.filter(source_variant=variant).count(),
            1,
        )

    def test_apply_to_target_creates_v2_without_repinning_scene(self) -> None:
        track, version_one = self._track_with_version(title="Original")
        scene = Scene.objects.create(project=self.project, title="Pinned scene", order=1)
        assignment = SceneMusic.objects.create(scene=scene, track=track, track_version=version_one)
        job, variant = self._completed_job()
        job.target_track = track
        job.save(update_fields=("target_track", "updated_at"))

        response = self.client.post(
            f"{self.root}generation-jobs/{job.id}/variants/{variant.id}/apply/",
            {
                "targetTrackId": track.id,
                "expectedTrackVersion": track.version,
                "title": "Regenerated",
                "author": "Craft AI",
                "tags": ["v2"],
                "makeActive": True,
            },
            format="json",
            **self._header(),
        )

        self.assertEqual(response.status_code, 200, response.json())
        track.refresh_from_db()
        assignment.refresh_from_db()
        versions = list(track.versions.order_by("version_number"))
        self.assertEqual([item.version_number for item in versions], [1, 2])
        self.assertEqual(track.active_version_id, versions[1].id)
        self.assertEqual(assignment.track_version_id, version_one.id)
        self.assertEqual(response.json()["trackVersion"], track.version)

    def test_job_action_flags_are_permission_aware_for_viewers(self) -> None:
        queued = MusicGenerationJob.objects.create(
            project=self.project,
            actor=self.owner,
            brief=_brief(),
            compiled_request={"contentMode": "instrumental"},
            provider="mock",
            model_name="deterministic-wav-v1",
            status=MusicJobStatus.QUEUED,
            stage=MusicJobStage.QUEUED,
            variant_count=1,
            idempotency_key="permission-queued",
            request_fingerprint="c" * 64,
        )
        failed = MusicGenerationJob.objects.create(
            project=self.project,
            actor=self.owner,
            brief=_brief(),
            compiled_request={"contentMode": "instrumental"},
            provider="mock",
            model_name="deterministic-wav-v1",
            status=MusicJobStatus.FAILED,
            stage=MusicJobStage.FAILED,
            variant_count=1,
            idempotency_key="permission-failed",
            request_fingerprint="d" * 64,
            error_code="MUSIC_PROVIDER_TIMEOUT",
            error_detail="raw upstream secret",
            error_retryable=True,
        )

        owner_queued = self.client.get(f"{self.root}generation-jobs/{queued.id}/", **self._header())
        viewer_queued = self.client.get(f"{self.root}generation-jobs/{queued.id}/", **self._header(self.viewer_key))
        owner_failed = self.client.get(f"{self.root}generation-jobs/{failed.id}/", **self._header())
        viewer_failed = self.client.get(f"{self.root}generation-jobs/{failed.id}/", **self._header(self.viewer_key))

        self.assertTrue(owner_queued.json()["canCancel"])
        self.assertFalse(viewer_queued.json()["canCancel"])
        self.assertTrue(owner_failed.json()["canRetry"])
        self.assertFalse(viewer_failed.json()["canRetry"])
        public_detail = owner_failed.json()["error"]["detail"]
        self.assertEqual(public_detail, "Music provider timed out.")
        self.assertNotIn("secret", public_detail)

    def test_assignment_payload_includes_prefetched_scene_context(self) -> None:
        track, version = self._track_with_version()
        location = Location.objects.create(project=self.project, name="Harbor")
        scenes = []
        for order in (1, 2):
            scene = Scene.objects.create(
                project=self.project,
                location=location,
                title=f"Harbor scene {order}",
                order=order,
                act=1,
                mood="tense",
                duration_seconds=40 + order,
                description=f"Scene summary {order}",
            )
            character = StudioCharacter.objects.create(
                project=self.project,
                user=self.owner_key,
                name=f"Character {order}",
            )
            SceneCharacter.objects.create(scene=scene, character=character)
            SceneMusic.objects.create(
                scene=scene,
                track=track,
                track_version=version,
                start_time_seconds=order,
            )
            scenes.append(scene)

        scene_character_table = SceneCharacter._meta.db_table.lower()
        assignments_url = f"{self.root}{track.id}/assignments/"
        with CaptureQueriesContext(connection) as assignment_queries:
            assignments_response = self.client.get(
                assignments_url,
                **self._header(),
            )

        self.assertEqual(assignments_response.status_code, 200)
        self.assertEqual(
            sum(
                scene_character_table in query["sql"].lower()
                for query in assignment_queries.captured_queries
            ),
            1,
        )
        items = assignments_response.json()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(
            set(items[0]["scene"]),
            {
                "sceneId",
                "number",
                "act",
                "title",
                "location",
                "summary",
                "mood",
                "durationSeconds",
                "characters",
            },
        )
        self.assertEqual(items[0]["scene"]["characters"], ["Character 1"])
        self.assertEqual(items[0]["scene"]["summary"], "Scene summary 1")
        self.assertEqual(items[0]["sceneNumber"], scenes[0].order)
        self.assertEqual(items[0]["sceneTitle"], scenes[0].title)
        self.assertEqual(items[0]["location"], location.name)
        self.assertEqual(items[0]["trackVersionNumber"], version.version_number)

        with CaptureQueriesContext(connection) as detail_queries:
            detail_response = self.client.get(
                f"{self.root}{track.id}/",
                **self._header(),
            )

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(
            sum(
                scene_character_table in query["sql"].lower()
                for query in detail_queries.captured_queries
            ),
            1,
        )
        detail_assignments = detail_response.json()["assignments"]
        self.assertEqual(
            [item["scene"]["sceneId"] for item in detail_assignments],
            [scene.id for scene in scenes],
        )

    def test_terminal_job_cancellation_returns_stable_conflict(self) -> None:
        job, _variant = self._completed_job()

        response = self.client.post(
            f"{self.root}generation-jobs/{job.id}/cancellation-request/",
            format="json",
            **self._header(),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "MUSIC_CANNOT_CANCEL")

    def test_assignment_remains_pinned_and_replacement_is_versioned(self) -> None:
        track, version_one = self._track_with_version()
        scene = Scene.objects.create(project=self.project, title="Opening", order=1)
        put_url = f"{self.root}{track.id}/assignments/"
        assigned = self.client.put(
            put_url,
            {
                "expectedTrackVersion": track.version,
                "items": [
                    {
                        "sceneId": scene.id,
                        "trackVersionId": str(version_one.id),
                        "startTimeSeconds": 3,
                    }
                ],
            },
            format="json",
            **self._header(),
        )
        self.assertEqual(assigned.status_code, 200, assigned.json())
        track.refresh_from_db()

        version_two = MusicTrackVersion.objects.create(
            track=track,
            version_number=2,
            asset=self._asset(name="v2.wav"),
            brief_snapshot=_brief(title="Version two"),
            created_by=self.owner,
        )
        track.active_version = version_two
        track.save(update_fields=("active_version", "updated_at"))

        detail = self.client.get(f"{self.root}{track.id}/", **self._header())
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["assignments"][0]["trackVersionId"],
            str(version_one.id),
        )

        stale = self.client.put(
            put_url,
            {"expectedTrackVersion": track.version - 1, "items": []},
            format="json",
            **self._header(),
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["currentVersion"], track.version)

    def test_track_and_job_ids_do_not_leak_across_projects(self) -> None:
        foreign_track, _ = self._track_with_version(project=self.other_project)
        response = self.client.get(
            f"{self.root}{foreign_track.id}/",
            **self._header(),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "MUSIC_TRACK_NOT_FOUND")
