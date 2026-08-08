"""Security regressions for paid project-scoped poster operations."""

from __future__ import annotations

import base64
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.poster import dashboard_views, facade, worker
from w_craft_back.movie.poster.errors import PosterProviderCircuitOpen
from w_craft_back.movie.poster.generation_guard import ensure_provider_circuit_closed
from w_craft_back.movie.poster.models import (
    PosterGenerationJob,
    PosterJobStatus,
    PosterProviderCircuit,
)
from w_craft_back.movie.poster.services import _PLACEHOLDER_PNG_BASE64
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project


def _user_with_token(username: str) -> tuple[User, str]:
    user = User.objects.create_user(username=username, password="pw")
    token = UserKey.objects.create(user=user)
    return user, str(token.key)


def _project(owner: User, title: str) -> Project:
    user_key = UserKey.objects.get(user=owner)
    project = Project.objects.create(
        owner=owner,
        user=user_key,
        title=title,
        description="",
        format="",
        annot="",
        desc="",
    )
    ProjectMember.objects.create(
        project=project,
        user=owner,
        role=ProjectMemberRole.OWNER,
    )
    return project


class RecordingProvider:
    name = "security-test"
    model_id = "recording-v1"

    def __init__(
        self,
        *,
        fail: bool = False,
        output_bytes: bytes | None = None,
    ):
        self.fail = fail
        self.output_bytes = output_bytes
        self.generate_calls: list[dict] = []
        self.edit_calls: list[dict] = []

    def generate(self, prompt: str, **kwargs) -> list[bytes]:
        self.generate_calls.append({"prompt": prompt, **kwargs})
        if self.fail:
            raise RuntimeError("provider unavailable")
        image = self.output_bytes or base64.b64decode(_PLACEHOLDER_PNG_BASE64)
        return [image]

    def edit(self, image_bytes: bytes, instruction: str, **kwargs) -> bytes:
        self.edit_calls.append(
            {
                "image_bytes": image_bytes,
                "instruction": instruction,
                **kwargs,
            }
        )
        if self.fail:
            raise RuntimeError("provider unavailable")
        return self.output_bytes or base64.b64decode(_PLACEHOLDER_PNG_BASE64)


@override_settings(POSTER_GENERATION_USE_MOCK=False)
class PosterGenerationSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.media_dir = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media_dir.cleanup)

        self.client = APIClient()
        self.owner, self.owner_token = _user_with_token("poster-owner")
        self.viewer, self.viewer_token = _user_with_token("poster-viewer")
        self.editor, self.editor_token = _user_with_token("poster-editor")
        self.project = _project(self.owner, "Poster security")
        ProjectMember.objects.create(
            project=self.project,
            user=self.viewer,
            role=ProjectMemberRole.VIEWER,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.editor,
            role=ProjectMemberRole.EDITOR,
        )
        self.generate_url = f"/api/projects/{self.project.id}/poster/generate/"
        self.edit_url = f"/api/projects/{self.project.id}/poster/edit/"
        self.generate_payload = {
            "prompt": "A neon city at night",
            "style": "cinematic",
            "format": "vertical",
        }

    @staticmethod
    def _headers(token: str, key: str = "request-1") -> dict[str, str]:
        return {
            "HTTP_X_USER_TOKEN": token,
            "HTTP_IDEMPOTENCY_KEY": key,
        }

    @staticmethod
    def _execute(response):
        return worker.execute_poster_job(response.json()["jobId"])

    def _mock_source_variant(self, project: Project | None = None) -> int:
        target = project or self.project
        result = facade.generate_poster(
            self.owner,
            target.id,
            prompt="Source poster",
            style="cinematic",
            format="vertical",
            idempotency_key=f"source-{target.id}",
            run_mock=True,
        )
        return result["variants"][0]["id"]

    def test_legacy_paid_routes_are_not_mounted(self):
        for path in (
            "/api/generate/poster/",
            "/api/generate/edit/",
            "/api/auth/poster/",
            "/api/auth/edit/",
        ):
            with self.subTest(path=path):
                response = self.client.post(path, {}, format="json")
                self.assertEqual(response.status_code, 404)

    def test_paid_routes_reject_get(self):
        for path in (self.generate_url, self.edit_url):
            with self.subTest(path=path):
                response = self.client.get(
                    path,
                    HTTP_X_USER_TOKEN=self.owner_token,
                )
                self.assertEqual(response.status_code, 405)

    def test_generate_and_edit_reject_anonymous_requests(self):
        generate = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="anonymous-generate",
        )
        edit = self.client.post(
            self.edit_url,
            {"source_variant_id": 1, "instruction": "Brighter"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="anonymous-edit",
        )
        self.assertEqual(generate.status_code, 401)
        self.assertEqual(edit.status_code, 401)
        self.assertEqual(PosterGenerationJob.objects.count(), 0)

    def test_body_token_is_not_accepted_for_poster_generation(self):
        response = self.client.post(
            self.generate_url,
            {**self.generate_payload, "token_user": self.owner_token},
            format="json",
            HTTP_IDEMPOTENCY_KEY="body-token",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(PosterGenerationJob.objects.count(), 0)

    @override_settings(POSTER_MAX_INPUT_BYTES=64)
    def test_declared_oversized_request_is_rejected_before_parsing(self):
        response = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            CONTENT_LENGTH=str(100_000),
            **self._headers(self.owner_token, "oversized-request"),
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "POSTER_IMAGE_TOO_LARGE")
        self.assertEqual(PosterGenerationJob.objects.count(), 0)

    def test_streamed_upload_limit_marker_returns_413(self):
        def mark_upload_exceeded(request):
            setattr(request._request, "_poster_upload_exceeded", True)

        with patch.object(
            dashboard_views,
            "_prepare_reference_upload",
            side_effect=mark_upload_exceeded,
        ):
            response = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "stream-limit"),
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "POSTER_IMAGE_TOO_LARGE")
        self.assertEqual(PosterGenerationJob.objects.count(), 0)

    def test_viewer_cannot_run_generation(self):
        response = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.viewer_token),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "PROJECT_ACCESS_DENIED")

    @override_settings(POSTER_GENERATION_USE_MOCK=True)
    def test_editor_with_run_generation_permission_is_allowed(self):
        response = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.editor_token),
        )
        self.assertEqual(response.status_code, 202)

    def test_idempotency_key_is_required(self):
        response = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "IDEMPOTENCY_KEY_REQUIRED")

    @override_settings(POSTER_PROVIDER_TIMEOUT_SECONDS=7)
    def test_duplicate_request_replays_without_second_provider_call(self):
        provider = RecordingProvider()
        with patch.object(worker, "resolve_provider_for_user", return_value=provider):
            first = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "same-request"),
            )
            self._execute(first)
            second = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "same-request"),
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertTrue(second.json()["idempotentReplay"])
        self.assertEqual(first.json()["jobId"], second.json()["jobId"])
        self.assertEqual(len(provider.generate_calls), 1)
        self.assertEqual(provider.generate_calls[0]["timeout"], 7.0)

    @override_settings(POSTER_GENERATION_USE_MOCK=True)
    def test_reusing_key_for_different_payload_returns_conflict(self):
        first = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "conflicting-key"),
        )
        changed = {**self.generate_payload, "prompt": "A different poster"}
        second = self.client.post(
            self.generate_url,
            changed,
            format="json",
            **self._headers(self.owner_token, "conflicting-key"),
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "IDEMPOTENCY_KEY_REUSED")

    @override_settings(POSTER_GENERATION_USE_MOCK=True)
    def test_active_job_blocks_another_project_user_call(self):
        first = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "active-first"),
        )
        PosterGenerationJob.objects.filter(pk=first.json()["jobId"]).update(
            status=PosterJobStatus.PROCESSING
        )

        second = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "active-second"),
        )
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["code"], "POSTER_CONCURRENCY_LIMIT")

    @override_settings(
        POSTER_GENERATION_USE_MOCK=True,
        POSTER_MAX_ACTIVE_JOBS_PER_USER=1,
    )
    def test_user_concurrency_limit_spans_projects(self):
        first = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "user-active-first"),
        )
        PosterGenerationJob.objects.filter(pk=first.json()["jobId"]).update(
            status=PosterJobStatus.PROCESSING,
        )
        other_project = _project(self.owner, "Other poster project")

        second = self.client.post(
            f"/api/projects/{other_project.id}/poster/generate/",
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "user-active-second"),
        )

        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["code"], "POSTER_CONCURRENCY_LIMIT")

    @override_settings(
        POSTER_GENERATION_USE_MOCK=True,
        POSTER_DAILY_QUOTA_PER_USER_PROJECT=1,
    )
    def test_rolling_quota_blocks_calls_after_limit(self):
        first = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "quota-first"),
        )
        self._execute(first)
        second = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "quota-second"),
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["code"], "POSTER_DAILY_QUOTA_EXCEEDED")

    @override_settings(
        POSTER_GENERATION_USE_MOCK=True,
        POSTER_DAILY_QUOTA_PER_USER_PROJECT=10,
        POSTER_DAILY_QUOTA_PER_USER=1,
    )
    def test_account_quota_spans_projects(self):
        first = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "account-quota-first"),
        )
        other_project = _project(self.owner, "Quota bypass project")
        second = self.client.post(
            f"/api/projects/{other_project.id}/poster/generate/",
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "account-quota-second"),
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["code"], "POSTER_DAILY_QUOTA_EXCEEDED")

    @override_settings(
        POSTER_CIRCUIT_FAILURE_THRESHOLD=2,
        POSTER_CIRCUIT_COOLDOWN_SECONDS=300,
    )
    def test_provider_circuit_opens_after_repeated_failures(self):
        provider = RecordingProvider(fail=True)
        with patch.object(worker, "resolve_provider_for_user", return_value=provider):
            first = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "circuit-first"),
            )
            first_job = self._execute(first)
            second = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "circuit-second"),
            )
            second_job = self._execute(second)
            third = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "circuit-third"),
            )
            third_job = self._execute(third)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(third.status_code, 202)
        self.assertEqual(first_job.status, PosterJobStatus.FAILED)
        self.assertEqual(second_job.status, PosterJobStatus.FAILED)
        self.assertEqual(third_job.error_code, "POSTER_PROVIDER_CIRCUIT_OPEN")
        self.assertEqual(len(provider.generate_calls), 2)

    @override_settings(POSTER_CIRCUIT_FAILURE_THRESHOLD=2)
    def test_invalid_provider_images_open_circuit(self):
        provider = RecordingProvider(output_bytes=b"not-an-image")
        with patch.object(worker, "resolve_provider_for_user", return_value=provider):
            first = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "invalid-output-first"),
            )
            first_job = self._execute(first)
            second = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "invalid-output-second"),
            )
            second_job = self._execute(second)
            third = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "invalid-output-third"),
            )
            third_job = self._execute(third)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(third.status_code, 202)
        self.assertEqual(first_job.error_code, "IMAGE_PROVIDER_BAD_RESPONSE")
        self.assertEqual(second_job.error_code, "IMAGE_PROVIDER_BAD_RESPONSE")
        self.assertEqual(third_job.error_code, "POSTER_PROVIDER_CIRCUIT_OPEN")
        self.assertEqual(len(provider.generate_calls), 2)

    def test_failed_idempotent_replay_preserves_http_status(self):
        provider = RecordingProvider(fail=True)
        with patch.object(worker, "resolve_provider_for_user", return_value=provider):
            first = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "failed-replay"),
            )
            failed_job = self._execute(first)
            replay = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "failed-replay"),
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(failed_job.status, PosterJobStatus.FAILED)
        self.assertEqual(replay.status_code, 503)
        self.assertEqual(replay.json()["code"], failed_job.error_code)
        self.assertEqual(len(provider.generate_calls), 1)

    @override_settings(POSTER_PROVIDER_TIMEOUT_SECONDS=7)
    def test_circuit_reserves_only_one_half_open_probe(self):
        circuit = PosterProviderCircuit.objects.create(
            provider_key="half-open-provider",
            failure_count=3,
            opened_until=timezone.now() - timedelta(seconds=1),
        )

        ensure_provider_circuit_closed(circuit.provider_key)
        circuit.refresh_from_db()
        self.assertGreater(circuit.opened_until, timezone.now())
        with self.assertRaises(PosterProviderCircuitOpen):
            ensure_provider_circuit_closed(circuit.provider_key)

    def test_viewer_cannot_edit_poster(self):
        source_variant_id = self._mock_source_variant()
        response = self.client.post(
            self.edit_url,
            {
                "source_variant_id": source_variant_id,
                "instruction": "Make it brighter",
            },
            format="json",
            **self._headers(self.viewer_token, "viewer-edit"),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "PROJECT_ACCESS_DENIED")

    @override_settings(POSTER_PROVIDER_TIMEOUT_SECONDS=9)
    def test_edit_uses_project_variant_and_provider_timeout(self):
        source_variant_id = self._mock_source_variant()
        provider = RecordingProvider()
        with patch.object(worker, "resolve_provider_for_user", return_value=provider):
            response = self.client.post(
                self.edit_url,
                {
                    "source_variant_id": source_variant_id,
                    "instruction": "Make it brighter",
                },
                format="json",
                **self._headers(self.owner_token, "successful-edit"),
            )
            self._execute(response)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(provider.edit_calls), 1)
        self.assertEqual(provider.edit_calls[0]["timeout"], 9.0)

    def test_edit_rejects_variant_from_another_project(self):
        other_project = _project(self.owner, "Other project")
        source_variant_id = self._mock_source_variant(other_project)
        response = self.client.post(
            self.edit_url,
            {
                "source_variant_id": source_variant_id,
                "instruction": "Make it brighter",
            },
            format="json",
            **self._headers(self.owner_token, "cross-project-edit"),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "POSTER_VARIANT_NOT_FOUND")

    @override_settings(
        CORS_ORIGIN_ALLOW_ALL=False,
        CORS_ALLOWED_ORIGINS=["http://frontend.test"],
    )
    def test_cors_preflight_allows_idempotency_header(self):
        response = self.client.options(
            self.generate_url,
            HTTP_ORIGIN="http://frontend.test",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS=(
                "content-type,idempotency-key,x-user-token"
            ),
        )
        self.assertEqual(response.status_code, 200)
        allowed = response["Access-Control-Allow-Headers"].lower()
        self.assertIn("idempotency-key", allowed)

    @override_settings(POSTER_GENERATION_USE_MOCK=True)
    def test_stale_processing_job_is_recovered(self):
        first = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "stale-first"),
        )
        first_job_id = first.json()["jobId"]
        PosterGenerationJob.objects.filter(pk=first_job_id).update(
            provider_started_at=timezone.now(),
            attempts=1,
            status=PosterJobStatus.PROCESSING,
            lease_expires_at=timezone.now(),
        )

        second = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "stale-second"),
        )
        self.assertEqual(second.status_code, 202)
        stale = PosterGenerationJob.objects.get(pk=first_job_id)
        self.assertEqual(stale.status, PosterJobStatus.FAILED)
        self.assertEqual(stale.error_code, "PROVIDER_OUTCOME_UNKNOWN")

    def test_persistence_failure_releases_job_lease(self):
        provider = RecordingProvider()
        circuit = PosterProviderCircuit.objects.create(
            provider_key="security-test:recording-v1",
            failure_count=3,
            opened_until=timezone.now() - timedelta(seconds=1),
        )
        with (
            patch.object(worker, "resolve_provider_for_user", return_value=provider),
            patch.object(worker, "complete_generation", side_effect=OSError("disk")),
        ):
            response = self.client.post(
                self.generate_url,
                self.generate_payload,
                format="json",
                **self._headers(self.owner_token, "persistence-failure"),
            )
            self._execute(response)
        self.assertEqual(response.status_code, 202)
        job = PosterGenerationJob.objects.get()
        self.assertEqual(job.status, PosterJobStatus.FAILED)
        self.assertEqual(job.error_code, "POSTER_RESULT_PERSISTENCE_FAILED")
        self.assertIsNone(job.lease_expires_at)
        circuit.refresh_from_db()
        self.assertEqual(circuit.failure_count, 0)
        self.assertIsNone(circuit.opened_until)
        self.assertIsNotNone(job.provider_started_at)

    @override_settings(POSTER_GENERATION_USE_MOCK=True)
    def test_selected_variant_is_exposed_by_project_detail(self):
        generated = self.client.post(
            self.generate_url,
            self.generate_payload,
            format="json",
            **self._headers(self.owner_token, "project-poster"),
        )
        self._execute(generated)
        detail = self.client.get(
            f"/api/projects/{self.project.id}/poster/jobs/{generated.json()['jobId']}/",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        variant = detail.json()["variants"][0]
        selected = self.client.patch(
            f"/api/projects/{self.project.id}/poster/select/",
            {"variant_id": variant["id"]},
            format="json",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        project = self.client.get(
            f"/api/projects/{self.project.id}/",
            HTTP_X_USER_TOKEN=self.owner_token,
        )
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(project.status_code, 200)
        self.assertEqual(project.json()["posterUrl"], variant["imageUrl"])

    @override_settings(POSTER_MAX_INPUT_BYTES=1)
    def test_edit_rejects_oversized_server_owned_source(self):
        source_variant_id = self._mock_source_variant()
        provider = RecordingProvider()
        with patch.object(worker, "resolve_provider_for_user", return_value=provider):
            response = self.client.post(
                self.edit_url,
                {
                    "source_variant_id": source_variant_id,
                    "instruction": "Make it brighter",
                },
                format="json",
                **self._headers(self.owner_token, "oversized-edit"),
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "POSTER_IMAGE_TOO_LARGE")
        self.assertEqual(provider.edit_calls, [])
