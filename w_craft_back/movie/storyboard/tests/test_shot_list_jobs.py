from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from io import StringIO
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from w_craft_back.movie.project.dashboard_models import (
    ProjectMember, ProjectMemberRole, Scene,
)
from w_craft_back.movie.storyboard.editor_drafts import save_editor_draft
from w_craft_back.movie.storyboard.errors import StoryboardError
from w_craft_back.movie.storyboard.models import (
    SceneStoryboardEditorDraft,
    SceneStoryboardShotListJob,
    SceneStoryboardShotListRequest,
)
from w_craft_back.movie.storyboard.shot_list_jobs import (
    ShotListLeaseLost,
    claim_shot_list_job,
    enqueue_shot_list,
    execute_shot_list_job,
    finalize_shot_list_job,
    recover_stale_shot_list_jobs,
)
from w_craft_back.movie.storyboard.source import source_from_scene
from w_craft_back.movie.storyboard.tests.test_api import make_project, make_user


MODEL = "gemini/gemini-2.5-flash"
PROVIDER = "w_craft_back.movie.storyboard.shot_list.LiteLLMShotListProvider"


def proposal_for(scene: Scene) -> dict:
    return {"shots": [{
        "title": "Анна входит", "description": "Анна замечает письмо.",
        "source_segment_ids": [source_from_scene(scene)["segments"][0]["id"]],
        "suggested_characters": [], "suggested_assets": [],
        "suggested_location": None, "suggested_framing": "medium_close",
    }]}


@override_settings(
    GEMINI_API_KEY="test-key", STORYBOARD_SHOT_LIST_MODEL=MODEL,
    STORYBOARD_SHOT_LIST_MODELS=MODEL,
    STORYBOARD_SHOT_LIST_THROTTLE_RATE="1000/min",
)
class ShotListJobTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner, cls.owner_key = make_user("shot-list-owner")
        cls.viewer, cls.viewer_key = make_user("shot-list-viewer")
        cls.editor, cls.editor_key = make_user("shot-list-editor")
        cls.outsider, cls.outsider_key = make_user("shot-list-outsider")
        cls.project = make_project(cls.owner, "Film")
        cls.other_project = make_project(cls.outsider, "Other")
        for user, role in (
            (cls.viewer, ProjectMemberRole.VIEWER),
            (cls.editor, ProjectMemberRole.EDITOR),
        ):
            ProjectMember.objects.create(project=cls.project, user=user, role=role)
        cls.scene = Scene.objects.create(
            project=cls.project, title="Комната", order=1,
            script_text="Анна входит. Она замечает письмо.",
        )
        cls.other_scene = Scene.objects.create(
            project=cls.other_project, title="Secret", order=1, script_text="Secret",
        )

    def setUp(self):
        self.client = APIClient()
        self.base = f"/api/projects/{self.project.pk}/storyboard/"
        self.create_url = f"{self.base}scenes/{self.scene.pk}/shot-list-jobs/"
        self.list_url = f"{self.base}shot-list-jobs/"
        self.loader = patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=SimpleNamespace(),
        )
        self.loader.start()
        self.addCleanup(self.loader.stop)
        provider = patch(f"{PROVIDER}.suggest", return_value=proposal_for(self.scene))
        self.provider = provider.start()
        self.addCleanup(provider.stop)

    def token(self, key=None):
        return {"HTTP_X_USER_TOKEN": str((key or self.owner_key).key)}

    def create(self, request_id=None, key=None, url=None, **changes):
        return self.client.post(url or self.create_url, {
            "requestId": str(request_id or uuid.uuid4()), "model": MODEL,
            "language": "ru", "maxShots": 16, "estimatedSeconds": 45,
            **changes,
        }, format="json", **self.token(key))

    def job(self, response=None):
        response = response or self.create()
        self.assertEqual(response.status_code, 202, response.content)
        return SceneStoryboardShotListJob.objects.get(pk=response.json()["jobId"])

    def save_empty(self, revision=0):
        return save_editor_draft(
            actor=self.owner, project_id=self.project.pk, scene_id=self.scene.pk,
            expected_revision=revision, mutation_id=uuid.uuid4(),
            payload={"schemaVersion": 1, "stage": "builder", "shots": []},
        )

    def test_request_returns_before_provider_and_reload_gets_saved_result(self):
        job = self.job()
        self.provider.assert_not_called()
        self.assertFalse(SceneStoryboardEditorDraft.objects.exists())
        self.assertEqual(job.expected_revision, 0)
        self.assertEqual(job.status, "queued")

        execute_shot_list_job(job.pk)
        self.provider.assert_called_once()
        response = APIClient().get(self.list_url, **self.token())
        self.assertEqual(response.status_code, 200, response.content)
        restored = response.json()["jobs"][0]
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["resultState"], "applied")
        self.assertEqual(restored["appliedRevision"], 1)
        self.assertIsNotNone(restored["startedAt"])
        self.assertIsNotNone(restored["finishedAt"])
        draft = SceneStoryboardEditorDraft.objects.get(scene=self.scene)
        self.assertEqual(draft.payload, restored["result"])
        self.assertEqual(draft.payload["stage"], "builder")
        shot = draft.payload["shots"][0]
        self.assertEqual(
            shot["keyframes"][0]["cameraIntent"]["framing"], "medium-close",
        )
        self.assertEqual(shot["source"]["document"]["sceneId"], self.scene.pk)
        self.assertEqual(len(shot["keyframes"]), 1)
        self.assertEqual(shot["keyframes"][0]["type"], "start")
        self.assertEqual(shot["transitions"], [])
        self.assertIn("Russian (ru)", self.provider.call_args.kwargs["prompt"])
        self.assertIsNone(execute_shot_list_job(job.pk))
        self.provider.assert_called_once()

    def test_revision_change_preserves_result_without_overwriting_manual_draft(self):
        job = self.job()
        self.save_empty()
        execute_shot_list_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.result_state, "pending")
        self.assertIsNone(job.applied_revision)
        self.assertTrue(job.result["shots"])
        self.assertEqual(SceneStoryboardEditorDraft.objects.get().payload["shots"], [])

    def test_explicit_apply_checks_revision_and_is_idempotent_after_later_edits(self):
        job = self.job()
        self.save_empty()
        execute_shot_list_job(job.pk)
        url = f"{self.list_url}{job.pk}/apply/"
        mutation_id = str(uuid.uuid4())
        stale = self.client.post(url, {
            "expectedRevision": 0, "mutationId": mutation_id,
        }, format="json", **self.token())
        self.assertEqual(stale.status_code, 409, stale.content)
        self.assertEqual(stale.json()["code"], "STORYBOARD_DRAFT_CONFLICT")
        applied = self.client.post(url, {
            "expectedRevision": 1, "mutationId": mutation_id,
        }, format="json", **self.token())
        self.assertEqual(applied.status_code, 200, applied.content)
        self.assertEqual(applied.json()["appliedRevision"], 2)
        self.save_empty(revision=2)
        replay = self.client.post(url, {
            "expectedRevision": 1, "mutationId": mutation_id,
        }, format="json", **self.token())
        self.assertEqual(replay.json(), applied.json())
        self.assertEqual(SceneStoryboardEditorDraft.objects.get().revision, 3)
        self.assertEqual(SceneStoryboardEditorDraft.objects.get().payload["shots"], [])

    def test_already_autoapplied_job_never_overwrites_later_reset(self):
        job = self.job()
        execute_shot_list_job(job.pk)
        self.save_empty(revision=1)
        response = self.client.post(f"{self.list_url}{job.pk}/apply/", {
            "expectedRevision": 2, "mutationId": str(uuid.uuid4()),
        }, format="json", **self.token())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["appliedRevision"], 1)
        self.assertEqual(SceneStoryboardEditorDraft.objects.get().revision, 2)

    def test_enqueued_snapshot_is_used_after_screenplay_changes(self):
        job = self.job()
        Scene.objects.filter(pk=self.scene.pk).update(script_text="Новый сценарий.")
        execute_shot_list_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.result_state, "pending")
        self.assertEqual(job.status, "succeeded")
        self.assertFalse(SceneStoryboardEditorDraft.objects.exists())
        self.assertIn("Анна входит.", self.provider.call_args.kwargs["prompt"])
        self.assertNotIn("Новый сценарий.", self.provider.call_args.kwargs["prompt"])

    def test_start_snapshots_current_saved_revision(self):
        self.save_empty()
        job = self.job()
        self.assertEqual(job.expected_revision, 1)
        execute_shot_list_job(job.pk)
        self.assertEqual(SceneStoryboardEditorDraft.objects.get().revision, 2)

    def test_request_id_retries_are_idempotent_and_parameters_cannot_change(self):
        request_id = uuid.uuid4()
        job = self.job(self.create(request_id))
        self.assertEqual(self.create(request_id).json()["jobId"], str(job.pk))
        execute_shot_list_job(job.pk)
        self.assertEqual(self.create(request_id).json()["jobId"], str(job.pk))
        self.assertEqual(self.create(request_id, maxShots=8).status_code, 400)
        self.assertEqual(SceneStoryboardShotListJob.objects.count(), 1)
        self.provider.assert_called_once()

    def test_active_job_reuse_remembers_secondary_request_even_after_completion(self):
        job = self.job()
        secondary_id = uuid.uuid4()
        reused = self.create(secondary_id, key=self.editor_key)
        self.assertEqual(reused.json()["jobId"], str(job.pk))
        self.assertEqual(SceneStoryboardShotListRequest.objects.count(), 2)
        execute_shot_list_job(job.pk)
        replay = self.create(secondary_id, key=self.editor_key)
        self.assertEqual(replay.json()["jobId"], str(job.pk))
        self.assertEqual(SceneStoryboardShotListJob.objects.count(), 1)

    def test_dismiss_during_generation_preserves_result_but_prevents_adoption(self):
        job = self.job()
        url = f"{self.list_url}{job.pk}/dismiss/"
        self.assertEqual(self.client.post(url, **self.token()).status_code, 200)
        execute_shot_list_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.result_state, "dismissed")
        self.assertIsNotNone(job.result)
        self.assertFalse(SceneStoryboardEditorDraft.objects.exists())
        replay = self.client.post(url, **self.token())
        self.assertEqual(replay.json()["resultState"], "dismissed")
        applied = self.client.post(f"{self.list_url}{job.pk}/apply/", {
            "expectedRevision": 0, "mutationId": str(uuid.uuid4()),
        }, format="json", **self.token())
        self.assertEqual(applied.status_code, 409)

    def test_auth_roles_project_and_scene_scopes(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 401)
        self.assertEqual(self.create(key=self.viewer_key).status_code, 403)
        self.assertEqual(self.create(key=self.outsider_key).status_code, 403)
        foreign_url = self.create_url.replace(
            f"scenes/{self.scene.pk}/", f"scenes/{self.other_scene.pk}/",
        )
        self.assertEqual(self.create(url=foreign_url).status_code, 404)
        job = self.job()
        detail = f"{self.list_url}{job.pk}/"
        self.assertEqual(
            self.client.get(detail, **self.token(self.viewer_key)).status_code, 200,
        )
        self.assertEqual(
            self.client.get(detail, **self.token(self.outsider_key)).status_code, 403,
        )
        other_detail = detail.replace(
            f"projects/{self.project.pk}/", f"projects/{self.other_project.pk}/",
        )
        self.assertEqual(
            self.client.get(other_detail, **self.token(self.outsider_key)).status_code,
            404,
        )
        self.assertEqual(self.client.post(
            f"{detail}dismiss/", **self.token(self.viewer_key),
        ).status_code, 403)
        self.assertEqual(self.client.post(f"{detail}apply/", {
            "expectedRevision": 0, "mutationId": str(uuid.uuid4()),
        }, format="json", **self.token(self.viewer_key)).status_code, 403)

    def test_permission_revoked_before_execution_prevents_provider_call(self):
        job = self.job(self.create(key=self.editor_key))
        ProjectMember.objects.filter(project=self.project, user=self.editor).delete()
        execute_shot_list_job(job.pk)
        self.provider.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "STORYBOARD_FORBIDDEN")

    def test_permission_revoked_during_provider_keeps_result_without_adoption(self):
        job = self.job(self.create(key=self.editor_key))
        proposal = proposal_for(self.scene)

        def finish(**kwargs):
            ProjectMember.objects.filter(
                project=self.project, user=self.editor,
            ).delete()
            return proposal

        self.provider.side_effect = finish
        execute_shot_list_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.result_state, "pending")
        self.assertIsNotNone(job.result)
        self.assertFalse(SceneStoryboardEditorDraft.objects.exists())

    def test_optional_adoption_failure_cannot_discard_a_completed_paid_result(self):
        job = self.job()
        with patch(
            "w_craft_back.movie.storyboard.shot_list_jobs._adopt",
            side_effect=StoryboardError(
                "Permission changed", code="STORYBOARD_FORBIDDEN", http_status=403,
            ),
        ):
            execute_shot_list_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.result_state, "pending")
        self.assertIsNotNone(job.result)
        self.assertFalse(SceneStoryboardEditorDraft.objects.exists())

    def test_latest_per_scene_list_and_old_job_detail(self):
        old = self.job()
        execute_shot_list_job(old.pk)
        new = self.job()
        response = self.client.get(self.list_url, **self.token())
        self.assertEqual(
            [job["jobId"] for job in response.json()["jobs"]], [str(new.pk)],
        )
        self.assertEqual(self.client.get(
            f"{self.list_url}{old.pk}/", **self.token(),
        ).json()["status"], "succeeded")

    def test_provider_validation_failure_is_saved_without_raw_provider_details(self):
        job = self.job()
        self.provider.return_value = {"shots": []}
        execute_shot_list_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "STORYBOARD_AI_BAD_RESPONSE")
        self.assertIsNone(job.result)
        self.assertFalse(SceneStoryboardEditorDraft.objects.exists())

    def test_expired_preprovider_lease_can_retry_but_old_worker_is_fenced(self):
        job = self.job()
        old_claim = claim_shot_list_job(job.pk)
        self.assertIsNone(claim_shot_list_job(job.pk))
        SceneStoryboardShotListJob.objects.filter(pk=job.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(recover_stale_shot_list_jobs()["requeued"], [job.pk])
        result = execute_shot_list_job(job.pk)
        with self.assertRaises(ShotListLeaseLost):
            finalize_shot_list_job(old_claim, result=deepcopy(result.result))
        self.provider.assert_called_once()
        self.assertEqual(SceneStoryboardEditorDraft.objects.get().revision, 1)

    def test_expired_after_provider_start_fails_without_automatic_paid_retry(self):
        job = self.job()
        claim_shot_list_job(job.pk)
        SceneStoryboardShotListJob.objects.filter(pk=job.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
            provider_started_at=timezone.now() - timedelta(seconds=30),
        )
        self.assertEqual(recover_stale_shot_list_jobs()["failed"], [job.pk])
        job.refresh_from_db()
        self.assertEqual(job.error_code, "STORYBOARD_AI_OUTCOME_UNKNOWN")
        self.assertIsNone(execute_shot_list_job(job.pk))
        self.provider.assert_not_called()

    def test_worker_storyboard_queue_executes_text_jobs(self):
        job = self.job()
        call_command("run_generation_worker", queue="storyboard", once=True,
                     stdout=StringIO())
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded")

    def test_rejects_invalid_request_uuid_and_estimate(self):
        self.assertEqual(self.create(request_id="not-a-uuid").status_code, 400)
        self.assertEqual(self.create(estimatedSeconds=0).status_code, 400)
        self.assertEqual(self.create(estimatedSeconds=3601).status_code, 400)
        self.assertFalse(SceneStoryboardShotListJob.objects.exists())


@override_settings(
    GEMINI_API_KEY="test-key", STORYBOARD_SHOT_LIST_MODEL=MODEL,
    STORYBOARD_SHOT_LIST_MODELS=MODEL,
)
class ShotListJobConcurrencyTests(TransactionTestCase):
    def test_simultaneous_start_creates_one_paid_job_and_two_durable_retries(self):
        owner, _ = make_user("shot-list-race")
        project = make_project(owner, "Concurrent")
        scene = Scene.objects.create(project=project, title="Scene", order=1)
        barrier = Barrier(2)

        def start(request_id):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return enqueue_shot_list(
                    actor=owner, project_id=project.pk, scene_id=scene.pk,
                    request_id=request_id, model=MODEL, language="ru", max_shots=16,
                    estimated_seconds=60,
                )
            finally:
                close_old_connections()

        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=SimpleNamespace(),
        ), ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(start, [uuid.uuid4(), uuid.uuid4()]))
        self.assertEqual(jobs[0]["jobId"], jobs[1]["jobId"])
        self.assertEqual(SceneStoryboardShotListJob.objects.count(), 1)
        self.assertEqual(SceneStoryboardShotListRequest.objects.count(), 2)
