"""Safety boundaries around durable video acceptance and external execution."""
import copy
import struct
import tempfile
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

from django.core import signing
from django.core.exceptions import ValidationError
from django.db.models.deletion import RestrictedError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory

from w_craft_back.movie.music.tests.helpers import make_project, make_user
from w_craft_back.movie.project.dashboard_models import (
    ProjectAsset, ProjectMember, Scene,
)
from w_craft_back.services.video_generation.contracts import (
    VideoDownload, VideoOperation, VideoPollResult, VideoProviderError,
)
from w_craft_back.storage_gateway import StorageGatewayError

from .. import lifecycle, services, worker
from ..errors import VideoError, VideoLeaseLost
from ..models import VideoGenerationJob
from ..storage import video_metadata


def mp4_bytes():
    def box(kind, content):
        return struct.pack(">I4s", len(content) + 8, kind) + content
    header = bytearray(84)
    struct.pack_into(">II", header, 76, 1280 << 16, 720 << 16)
    movie = bytes(12) + struct.pack(">II", 1000, 6000) + bytes(80)
    return (box(b"ftyp", b"isom" + bytes(8))
            + box(b"moov", box(b"mvhd", movie)
                  + box(b"trak", box(b"tkhd", bytes(header))))
            + box(b"mdat", bytes(32)))


class VideoJobsTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings = override_settings(MEDIA_ROOT=self.media.name)
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.owner = make_user("video-owner")
        self.viewer = make_user("video-viewer")
        self.other = make_user("video-other")
        self.project = make_project(self.owner)
        ProjectMember.objects.create(project=self.project, user=self.viewer,
                                     role="viewer")
        self.scene = Scene.objects.create(project=self.project, title="One",
                                          order=1, description="Walk to the door")
        self.asset = ProjectAsset.objects.create(
            project=self.project, asset_type="storyboard", file="frame.png")
        self.source = {
            "sourceKind": "editor_draft", "sourceShotId": "plan-one",
            "sceneId": self.scene.pk, "title": "Door", "order": 1,
            "description": "Opens the door", "prompt": "Scene: opens the door",
            "durationSeconds": 5, "aspectRatio": "16:9",
            "startFrame": {"assetId": self.asset.pk, "label": "Start"},
            "endFrame": None, "fingerprint": "a" * 64, "ready": True,
            "blockers": [], "assetIds": [self.asset.pk],
            "snapshot": {"scene": {"version": 1}, "shot": {"text": "Door"},
                         "characters": [], "references": []},
        }
        self.route = {
            "generationAccess": {"version": 1, "source": "personal",
                                 "ownerId": self.owner.pk, "modality": "video",
                                 "operation": "generate", "candidates": []},
            "modelKey": "gemini:veo", "modelLabel": "Veo", "provider": "gemini",
            "durationSeconds": 6, "aspectRatio": "16:9", "resolution": "720p",
            "modelSpec": {"durations": [4, 6, 8]},
        }
        self.source_mock = patch.object(
            services, "get_source", side_effect=lambda *a: copy.deepcopy(self.source),
        )
        self.source_mock.start()
        self.addCleanup(self.source_mock.stop)
        self.route_mock = patch("w_craft_back.personal_api.access.resolve_video_access",
                                side_effect=lambda *a, **kw: copy.deepcopy(self.route))
        self.route_mock.start()
        self.addCleanup(self.route_mock.stop)
        self.source_mode = patch.object(
            services, "generation_source", return_value="personal",
        )
        self.source_mode.start()
        self.addCleanup(self.source_mode.stop)

    def token(self, **overrides):
        intent = {"sceneId": self.scene.pk, "sourceKind": "editor_draft",
                  "sourceShotId": "plan-one", "motionPrompt": "Slow push"}
        return signing.dumps(
            {"actorId": self.owner.pk, "projectId": self.project.pk,
             "intent": intent, "sourceFingerprint": self.source["fingerprint"],
             "route": copy.deepcopy(self.route), **overrides},
            salt=services.PREFLIGHT_SALT, compress=True,
        )

    def create(self, payload=None):
        result = services.create_job(self.owner, self.project.pk, payload or {
            "preflightToken": self.token(), "clientRequestId": str(uuid.uuid4()),
        })
        return VideoGenerationJob.objects.get(pk=result["job"]["id"])

    def test_preflight_discloses_duration_and_no_personal_model_selector(self):
        result = services.options(self.owner, self.project.pk, {
            "sceneId": self.scene.pk, "sourceKind": "editor_draft",
            "sourceShotId": "plan-one", "motionPrompt": "slow",
        })
        self.assertEqual(result["models"], [])
        self.assertTrue(result["canGenerate"])
        self.assertEqual(result["requestedDurationSeconds"], 5)
        self.assertEqual(result["effectiveDurationSeconds"], 6)
        self.assertTrue(result["durationAdjusted"])
        self.scene.refresh_from_db()
        self.assertNotEqual(self.scene.duration_seconds, 6)

    def test_personal_mode_rejects_model_and_source_override(self):
        data = {"sceneId": self.scene.pk, "sourceKind": "editor_draft",
                "sourceShotId": "plan-one"}
        for extra in ({"modelId": "other"}, {"generationSource": "platform"}):
            with self.assertRaises(VideoError):
                services.options(self.owner, self.project.pk, data | extra)

    def test_request_is_idempotent_and_changed_token_conflicts(self):
        payload = {"preflightToken": self.token(), "clientRequestId": str(uuid.uuid4())}
        first = services.create_job(self.owner, self.project.pk, payload)
        second = services.create_job(self.owner, self.project.pk, payload)
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.assertTrue(second["idempotentReplay"])
        with self.assertRaises(VideoError) as error:
            services.create_job(self.owner, self.project.pk,
                                payload | {"preflightToken": self.token(extra=True)})
        self.assertEqual(error.exception.code, "VIDEO_IDEMPOTENCY_CONFLICT")
        self.assertEqual(VideoGenerationJob.objects.count(), 1)

    def test_source_and_route_changes_require_new_preflight(self):
        payload = {"preflightToken": self.token(), "clientRequestId": str(uuid.uuid4())}
        self.source["fingerprint"] = "b" * 64
        with self.assertRaises(VideoError) as error:
            self.create(payload)
        self.assertEqual(error.exception.code, "VIDEO_SOURCE_CHANGED")
        self.source["fingerprint"] = "a" * 64
        self.route["durationSeconds"] = 8
        with self.assertRaises(VideoError) as error:
            self.create(payload)
        self.assertEqual(error.exception.code, "VIDEO_ROUTE_CHANGED")

    def test_foreign_actor_or_viewer_cannot_enqueue(self):
        for actor in (self.viewer, self.other):
            with self.assertRaises(VideoError):
                services.create_job(actor, self.project.pk, {
                    "preflightToken": self.token(),
                    "clientRequestId": str(uuid.uuid4()),
                })
        self.assertEqual(VideoGenerationJob.objects.count(), 0)

    def test_input_snapshot_and_assets_are_protected(self):
        job = self.create()
        job.input_snapshot = {"description": "replacement"}
        with self.assertRaises(ValidationError):
            job.save()
        with self.assertRaises(RestrictedError):
            self.asset.delete()

    def test_removed_source_preserves_history_and_staleness(self):
        job = self.create()
        with patch.object(services, "collect_sources", return_value=[]):
            result = services.workspace(self.owner, self.project.pk)
        self.assertEqual(len(result["archivedShots"]), 1)
        old = result["archivedShots"][0]["jobs"][0]
        self.assertTrue(old["stale"])
        self.assertEqual(old["staleReasons"], ["source_removed"])
        self.assertTrue(VideoGenerationJob.objects.filter(pk=job.pk).exists())

    def test_expired_lease_with_unknown_post_never_requeues(self):
        job = self.create()
        claimed = lifecycle.claim_next_video_job()
        VideoGenerationJob.objects.filter(pk=job.pk).update(
            provider_started_at=timezone.now(), stage="submitting",
            lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(lifecycle.recover_stale_video_jobs(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, "outcome_unknown")
        self.assertIsNone(lifecycle.claim_next_video_job())
        with self.assertRaises(VideoLeaseLost):
            lifecycle.update_owned(job.pk, claimed.lease_token, status="completed")

    def test_known_operation_recovers_without_resubmit(self):
        job = self.create()
        lifecycle.claim_next_video_job()
        VideoGenerationJob.objects.filter(pk=job.pk).update(
            provider_started_at=timezone.now(), operation_id="operations/known",
            lease_expires_at=timezone.now() - timedelta(seconds=1))
        lifecycle.recover_stale_video_jobs()
        adapter = Mock()
        operation = VideoOperation("operations/known", "gemini", self.route["modelKey"])
        adapter.poll.return_value = VideoPollResult("processing", operation)
        with patch.object(worker, "_adapter", return_value=adapter):
            worker.execute_next_video_job()
        adapter.submit.assert_not_called()
        adapter.poll.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.operation_id, "operations/known")
        self.assertIsNone(job.lease_token)

    def test_lost_submit_response_is_terminal_and_no_automatic_retry(self):
        job = self.create()
        adapter = Mock()
        adapter.submit.side_effect = VideoProviderError("TIMEOUT", outcome_unknown=True)
        with patch.object(worker, "_adapter", return_value=adapter), \
                patch.object(worker, "_request"):
            worker.execute_next_video_job()
            self.assertIsNone(worker.execute_next_video_job())
        job.refresh_from_db()
        self.assertEqual(job.status, "outcome_unknown")
        self.assertFalse(job.error_retryable)
        adapter.submit.assert_called_once()

    def test_success_preserves_selection_until_explicit_choice(self):
        job = self.create()
        operation = VideoOperation("operations/clip", "gemini", self.route["modelKey"])
        adapter = Mock()
        adapter.submit.return_value = operation
        adapter.poll.return_value = VideoPollResult(
            "succeeded", operation, "https://private/result",
        )
        adapter.download.return_value = VideoDownload(mp4_bytes())
        with patch.object(worker, "_adapter", return_value=adapter), \
                patch.object(worker, "_request"):
            worker.execute_next_video_job()
            VideoGenerationJob.objects.filter(pk=job.pk).update(
                next_poll_at=timezone.now(),
            )
            worker.execute_next_video_job()
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertIsNone(job.video_shot.final_asset_id)
        self.assertEqual(job.result_asset.metadata["durationSeconds"], 6)
        shot = job.video_shot
        payload = {"jobId": str(job.pk), "expectedVideoShotVersion": shot.version}
        with self.assertRaises(VideoError):
            services.update_shot(
                self.viewer, self.project.pk, shot.pk, payload, select=True,
            )
        selected = services.update_shot(
            self.owner, self.project.pk, shot.pk, payload, select=True,
        )
        self.assertEqual(selected["selectedJobId"], str(job.pk))
        with self.assertRaises(VideoError) as error:
            services.update_shot(
                self.owner, self.project.pk, shot.pk, payload, select=True,
            )
        self.assertEqual(error.exception.code, "VIDEO_VERSION_CONFLICT")

    def test_download_failure_reuses_same_paid_operation(self):
        job = self.create()
        operation = VideoOperation("operations/clip", "gemini", self.route["modelKey"])
        VideoGenerationJob.objects.filter(pk=job.pk).update(operation_id=operation.id)
        adapter = Mock()
        adapter.poll.return_value = VideoPollResult(
            "succeeded", operation, "https://private/result",
        )
        adapter.download.side_effect = OSError("broken download")
        with patch.object(worker, "_adapter", return_value=adapter):
            worker.execute_next_video_job()
            VideoGenerationJob.objects.filter(pk=job.pk).update(
                next_poll_at=timezone.now(),
            )
            worker.execute_next_video_job()
        self.assertEqual(adapter.download.call_count, 2)
        adapter.submit.assert_not_called()

    def test_permission_revocation_stops_worker_before_provider(self):
        job = self.create()
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        with patch.object(worker, "create_adapter") as adapter:
            worker.execute_next_video_job()
        adapter.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.error_code, "VIDEO_ACCESS_REVOKED")

    def test_definite_preaccept_failure_uses_next_approved_route(self):
        first = {key: value for key, value in self.route.items()
                 if key != "generationAccess"}
        second = {**first, "modelKey": "gemini:fast", "modelLabel": "Fast"}
        self.route["candidateRoutes"] = [first, second]
        self.route["generationAccess"]["candidates"] = [
            {"id": "one"}, {"id": "two"},
        ]
        job = self.create()
        adapter = Mock()
        adapter.submit.side_effect = [
            VideoProviderError("VIDEO_QUOTA_EXCEEDED", safe_to_fallback=True),
            VideoOperation("operations/second", "gemini", "gemini:fast"),
        ]
        with patch.object(worker, "_adapter", return_value=adapter), \
                patch.object(worker, "_request"):
            worker.execute_next_video_job()
            job.refresh_from_db()
            self.assertEqual(job.status, "queued")
            self.assertEqual(job.execution_candidate_index, 1)
            self.assertIsNone(job.provider_started_at)
            worker.execute_next_video_job()
        job.refresh_from_db()
        self.assertEqual(job.operation_id, "operations/second")
        self.assertEqual(adapter.submit.call_count, 2)
        payload = services.job_payload(job, source=self.source)
        self.assertEqual(payload["modelId"], "gemini:fast")

    def test_ambiguous_failure_does_not_try_approved_backup(self):
        self.route["candidateRoutes"] = [{}, {}]
        self.route["generationAccess"]["candidates"] = [
            {"id": "one"}, {"id": "two"},
        ]
        job = self.create()
        adapter = Mock()
        adapter.submit.side_effect = VideoProviderError(
            "VIDEO_TIMEOUT", outcome_unknown=True,
        )
        with patch.object(worker, "_adapter", return_value=adapter), \
                patch.object(worker, "_request"):
            worker.execute_next_video_job()
            self.assertIsNone(worker.execute_next_video_job())
        job.refresh_from_db()
        self.assertEqual(job.execution_candidate_index, 0)
        self.assertEqual(job.status, "outcome_unknown")
        adapter.submit.assert_called_once()

    def test_http_requires_header_and_blocks_viewer_mutation(self):
        client = APIClient()
        url = f"/api/projects/{self.project.pk}/video/options/"
        self.assertIn(client.post(url, {}, format="json").status_code, (401, 403))
        client.force_authenticate(self.viewer)
        with patch.object(services, "collect_sources", return_value=[]):
            result = client.get(f"/api/projects/{self.project.pk}/video/workspace/")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["permissions"]["canRunGeneration"])
        result = client.post(
            f"/api/projects/{self.project.pk}/video/jobs/",
            {"preflightToken": self.token(), "clientRequestId": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(result.status_code, 403)

    def test_storage_rejects_fake_or_malformed_video(self):
        self.assertEqual(video_metadata(mp4_bytes(), "video/mp4")["width"], 1280)
        for payload in (b"<html>no video</html>", mp4_bytes()[:-2], bytes(100)):
            with self.assertRaises(StorageGatewayError):
                video_metadata(payload, "video/mp4")

    def test_signed_result_supports_byte_ranges(self):
        from w_craft_back.storage_gateway import signed_url_for_file
        from ..storage import store_generated_video
        stored, metadata = store_generated_video(
            mp4_bytes(), "video/mp4", self.project.pk,
        )
        asset = ProjectAsset.objects.create(project=self.project, asset_type="video",
                                            file=stored.storage_key, metadata={
                                                **metadata, "mimeType": "video/mp4"})
        request = APIRequestFactory().get("/")
        request.user = self.owner
        url = signed_url_for_file(asset.file, request, project=self.project)
        client = APIClient()
        response = client.get(url, HTTP_RANGE="bytes=0-15")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), mp4_bytes()[:16])
        response = client.get(url, HTTP_RANGE="bytes=999999-")
        self.assertEqual(response.status_code, 416)

    def test_project_cascade_releases_only_definitely_unsubmitted_jobs(self):
        queued = self.create()
        self.source["sourceShotId"] = "plan-two"
        sent = self.create({
            "preflightToken": self.token(intent={
                "sceneId": self.scene.pk, "sourceKind": "editor_draft",
                "sourceShotId": "plan-two", "motionPrompt": "slow",
            }),
            "clientRequestId": str(uuid.uuid4()),
        })
        VideoGenerationJob.objects.filter(pk=sent.pk).update(
            provider_started_at=timezone.now(), operation_id="operations/paid",
        )
        target = "w_craft_back.movie.video.signals.release_video_generation"
        with patch(target) as release:
            self.project.delete()
        self.assertFalse(VideoGenerationJob.objects.filter(pk=queued.pk).exists())
        self.assertEqual(release.call_count, 1)
        self.assertEqual(release.call_args.args[0].pk, queued.pk)

    def test_lease_loss_during_provider_error_does_not_stop_worker(self):
        job = self.create()
        adapter = Mock()

        def lose_lease(_request):
            VideoGenerationJob.objects.filter(pk=job.pk).update(
                lease_expires_at=timezone.now() - timedelta(seconds=1),
            )
            lifecycle.recover_stale_video_jobs()
            raise VideoProviderError("VIDEO_TIMEOUT", outcome_unknown=True)

        adapter.submit.side_effect = lose_lease
        with patch.object(worker, "_adapter", return_value=adapter), \
                patch.object(worker, "_request"):
            result = worker.execute_next_video_job()
        self.assertEqual(result.pk, job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "outcome_unknown")

    def test_revocation_after_initial_access_check_blocks_mutations(self):
        job = self.create()
        original = services.project_access

        def revoke_after_check(*args, **kwargs):
            project = original(*args, **kwargs)
            type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
            return project

        with patch.object(services, "project_access", side_effect=revoke_after_check):
            with self.assertRaises(VideoError):
                services.update_shot(self.owner, self.project.pk, job.video_shot_id, {
                    "motionPrompt": "changed", "expectedVideoShotVersion": 2,
                })
        job.video_shot.refresh_from_db()
        self.assertEqual(job.video_shot.motion_prompt, "Slow push")

    def test_removing_frame_generation_retains_pinned_asset(self):
        from w_craft_back.movie.storyboard.models import StoryboardEditorFrameJob
        self.create()
        frame_job = StoryboardEditorFrameJob.objects.create(
            scene=self.scene, actor=self.owner, shot_id="plan-one",
            keyframe_id="start", request_id=uuid.uuid4(), request_parameters={},
            expected_revision=1, input_fingerprint="a" * 64,
            request_snapshot={}, provider_snapshot={}, model="test",
            asset=self.asset, status="succeeded",
        )
        with self.captureOnCommitCallbacks(execute=True):
            frame_job.delete()
        self.assertTrue(ProjectAsset.objects.filter(pk=self.asset.pk).exists())

    def test_stale_selected_video_does_not_count_as_current_progress(self):
        from w_craft_back.movie.project.progress_service import (
            calculate_project_progress,
        )
        from w_craft_back.movie.project.dashboard_models import VideoShot
        job = self.create()
        asset = ProjectAsset.objects.create(
            project=self.project, asset_type="video", file="result.mp4",
        )
        VideoGenerationJob.objects.filter(pk=job.pk).update(
            result_asset=asset, status="completed",
        )
        VideoShot.objects.filter(pk=job.video_shot_id).update(final_asset=asset)
        sources = [{"id": self.scene.pk, "shots": [self.source]}]
        with patch("w_craft_back.movie.video.sources.collect_sources",
                   return_value=sources):
            self.assertEqual(calculate_project_progress(self.project).video, 1)
            self.source["fingerprint"] = "b" * 64
            result = calculate_project_progress(self.project)
        self.assertEqual(result.video, 0)
        self.assertEqual(result.video_total_count, 1)

    def test_download_failure_does_not_refund_an_accepted_operation(self):
        job = self.create()
        VideoGenerationJob.objects.filter(pk=job.pk).update(
            operation_id="operations/accepted", provider_started_at=timezone.now(),
        )
        claimed = lifecycle.claim_next_video_job()
        with patch("w_craft_back.credits.video.release_video_generation") as release:
            lifecycle.finish_error(claimed, "VIDEO_STORAGE_FAILED", "Download failed")
        release.assert_not_called()

    def test_full_length_storyboard_identifier_can_be_accepted(self):
        identifier = "plan-" + "x" * 251
        self.source["sourceShotId"] = identifier
        preflight = services.options(self.owner, self.project.pk, {
            "sceneId": self.scene.pk, "sourceKind": "editor_draft",
            "sourceShotId": identifier, "motionPrompt": "slow",
        })
        self.assertTrue(preflight["canGenerate"])
        job = self.create({
            "preflightToken": preflight["preflightToken"],
            "clientRequestId": str(uuid.uuid4()),
        })
        self.assertEqual(job.video_shot.source_shot_id, identifier)
        with self.assertRaises(VideoError):
            services.options(self.owner, self.project.pk, {
                "sceneId": self.scene.pk, "sourceKind": "editor_draft",
                "sourceShotId": identifier + "x",
            })

    def test_accepted_operation_deadline_is_terminal_without_another_call(self):
        job = self.create()
        now = timezone.now()
        VideoGenerationJob.objects.filter(pk=job.pk).update(
            operation_id="operations/slow",
            provider_started_at=now - worker.MAX_OPERATION_AGE,
        )
        release_target = "w_craft_back.credits.video.release_video_generation"
        with patch.object(worker, "_adapter") as adapter, \
                patch.object(worker.timezone, "now", return_value=now), \
                patch(release_target) as release:
            worker.execute_next_video_job()
            self.assertIsNone(worker.execute_next_video_job())
        adapter.assert_not_called()
        release.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "outcome_unknown")
        self.assertEqual(job.error_code, "VIDEO_OPERATION_TIMEOUT")
        self.assertFalse(job.error_retryable)
        self.assertEqual(job.operation_id, "operations/slow")
        self.assertEqual(job.input_assets.count(), 1)

    def test_accepted_operation_can_poll_until_deadline(self):
        job = self.create()
        now = timezone.now()
        VideoGenerationJob.objects.filter(pk=job.pk).update(
            operation_id="operations/slow",
            provider_started_at=now - worker.MAX_OPERATION_AGE
            + timedelta(seconds=1),
        )
        operation = VideoOperation("operations/slow", "gemini", "gemini:veo")
        adapter = Mock()
        adapter.poll.return_value = VideoPollResult("processing", operation)
        with patch.object(worker, "_adapter", return_value=adapter), \
                patch.object(worker.timezone, "now", return_value=now):
            worker.execute_next_video_job()
        adapter.poll.assert_called_once()
        adapter.submit.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "processing")

    def test_other_request_id_cannot_duplicate_an_active_shot(self):
        token = self.token()
        first_id = str(uuid.uuid4())
        first = services.create_job(self.owner, self.project.pk, {
            "preflightToken": token, "clientRequestId": first_id,
        })
        with self.assertRaises(VideoError) as error:
            services.create_job(self.owner, self.project.pk, {
                "preflightToken": token, "clientRequestId": str(uuid.uuid4()),
            })
        self.assertEqual(error.exception.code, "VIDEO_GENERATION_ACTIVE")
        replay = services.create_job(self.owner, self.project.pk, {
            "preflightToken": token, "clientRequestId": first_id,
        })
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["job"]["id"], first["job"]["id"])
        self.assertEqual(VideoGenerationJob.objects.count(), 1)
