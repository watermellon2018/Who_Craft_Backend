from datetime import timedelta
from io import StringIO
import shutil
import tempfile
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterGenerationJob,
    GenerationJobStatus,
    GenerationJobType,
    StudioCharacter,
)
from w_craft_back.character_studio.services.errors import (
    GenerationBudgetExceededError,
    GenerationConcurrencyLimitError,
    ValidationError,
)
from w_craft_back.character_studio.services.generation_lifecycle import (
    JobLease,
    build_generation_preview,
    claim_job,
    fail_job,
    recover_stale_jobs,
)
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.character_studio.services.providers import MockProvider
from w_craft_back.character_studio.tests import CharacterStudioTestCase
from w_craft_back.movie.project import policy, project_mutations
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.serializers import ProjectUpdateSerializer
from w_craft_back.profile.models import UserProfile


PROVIDER_FACTORY = (
    "w_craft_back.character_studio.services.generation_service."
    "get_image_provider"
)


class CountingProvider(MockProvider):
    def __init__(self):
        self.calls = 0
        self.atomic_depths = []

    def generate_character_variants(
        self,
        job,
        compiled_prompt,
        variant_count,
    ):
        self.calls += 1
        self.atomic_depths.append(len(connection.atomic_blocks))
        return super().generate_character_variants(
            job,
            compiled_prompt,
            variant_count,
        )

    def generate_from_reference(
        self,
        job,
        compiled_prompt,
        reference_image_bytes,
        mime_type,
        variant_count,
    ):
        self.calls += 1
        self.atomic_depths.append(len(connection.atomic_blocks))
        return super().generate_from_reference(
            job,
            compiled_prompt,
            reference_image_bytes,
            mime_type,
            variant_count,
        )


class CharacterGenerationLifecycleTests(CharacterStudioTestCase):
    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)
        super().tearDown()

    def test_provider_io_runs_without_service_atomic_block(self):
        character = self.create_character()
        provider = CountingProvider()
        baseline_atomic_depth = len(connection.atomic_blocks)

        with patch(PROVIDER_FACTORY, return_value=provider):
            job = CharacterGenerationService().create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {"variant_count": 1},
            )

        self.assertEqual(job.status, GenerationJobStatus.COMPLETED)
        self.assertEqual(provider.atomic_depths, [baseline_atomic_depth])
        self.assertIsNone(job.lease_token)
        self.assertIsNone(job.lease_expires_at)
        self.assertEqual(job.attempts, 1)

    def test_idempotency_key_replays_without_second_provider_call(self):
        character = self.create_character()
        provider = CountingProvider()
        service = CharacterGenerationService()
        params = {
            "variant_count": 1,
            "_idempotency_key": "character-create-42",
        }

        with patch(PROVIDER_FACTORY, return_value=provider):
            first = service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                params,
            )
            replay = service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                params,
            )
            with self.assertRaises(ValidationError):
                service.create_initial_variants(
                    self.user_key,
                    self.project.id,
                    character.character_id,
                    {
                        "variant_count": 2,
                        "_idempotency_key": "character-create-42",
                    },
                )

        self.assertEqual(replay.job_id, first.job_id)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(
            CharacterGenerationJob.objects.filter(
                idempotency_key="character-create-42"
            ).count(),
            1,
        )

    def test_idempotency_header_is_forwarded_by_generation_view(self):
        character = self.create_character()
        provider = CountingProvider()
        client = APIClient()
        url = (
            f"/api/projects/{self.project.id}/characters/"
            f"{character.character_id}/generate-initial-variants"
        )
        headers = {
            "HTTP_X_USER_TOKEN": str(self.user_key.key),
            "HTTP_IDEMPOTENCY_KEY": "api-request-42",
        }

        with patch(PROVIDER_FACTORY, return_value=provider):
            first = client.post(
                url,
                {"variant_count": 1},
                format="json",
                **headers,
            )
            replay = client.post(
                url,
                {"variant_count": 1},
                format="json",
                **headers,
            )
            conflict = client.post(
                url,
                {"variant_count": 2},
                format="json",
                **headers,
            )

        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(replay.status_code, 202, replay.content)
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertEqual(first.json()["job_id"], replay.json()["job_id"])
        self.assertEqual(provider.calls, 0)

        polling = client.get(
            f"/api/generation-jobs/{first.json()['job_id']}",
            HTTP_X_USER_TOKEN=str(self.user_key.key),
        )
        self.assertEqual(polling.status_code, 200, polling.content)
        for internal_field in (
            "lease_token",
            "request_hash",
            "idempotency_key",
            "compiled_prompt",
            "compiled_metadata",
        ):
            self.assertNotIn(internal_field, polling.json())

    def test_generation_view_requires_idempotency_key_before_provider_call(self):
        character = self.create_character()
        provider = CountingProvider()
        client = APIClient()

        with patch(PROVIDER_FACTORY, return_value=provider):
            response = client.post(
                (
                    f"/api/projects/{self.project.id}/characters/"
                    f"{character.character_id}/generate-initial-variants"
                ),
                {"variant_count": 1},
                format="json",
                HTTP_X_USER_TOKEN=str(self.user_key.key),
            )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error_code"], "IDEMPOTENCY_KEY_REQUIRED")
        self.assertEqual(provider.calls, 0)

    def test_generation_preview_has_no_provider_side_effect(self):
        character = self.create_character()
        self.project.generation_settings = {"image_generation_model": "mock"}
        self.project.save(update_fields=["generation_settings"])
        client = APIClient()

        with patch(PROVIDER_FACTORY) as provider_factory:
            response = client.get(
                (
                    f"/api/projects/{self.project.id}/characters/"
                    f"{character.character_id}/generation-preview"
                ),
                {"image_types": "portrait,full_body,scene"},
                HTTP_X_USER_TOKEN=str(self.user_key.key),
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["provider"], "mock")
        self.assertEqual(response.json()["mode"], "offline")
        self.assertEqual(response.json()["provider_call_count"], 3)
        self.assertEqual(response.json()["estimated_cost_usd"], "0")
        provider_factory.assert_not_called()

    @override_settings(CHARACTER_STUDIO_MAX_ACTIVE_PER_PROJECT=1)
    def test_project_concurrency_limit_blocks_new_provider_call(self):
        character = self.create_character()
        CharacterGenerationJob.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            actor=self.user_key,
            job_type=GenerationJobType.EDIT_VARIANTS,
            status=GenerationJobStatus.PROCESSING,
            variant_count=1,
            provider="mock",
        )
        provider = CountingProvider()

        with patch(PROVIDER_FACTORY, return_value=provider):
            with self.assertRaises(GenerationConcurrencyLimitError):
                CharacterGenerationService().create_initial_variants(
                    self.user_key,
                    self.project.id,
                    character.character_id,
                    {
                        "variant_count": 1,
                        "_idempotency_key": "concurrency-limit-test",
                    },
                )

        self.assertEqual(provider.calls, 0)

    @override_settings(
        CHARACTER_STUDIO_DAILY_BUDGET_PER_USER=1,
        CHARACTER_STUDIO_DAILY_BUDGET_PER_PROJECT=10,
    )
    def test_user_daily_budget_blocks_new_paid_provider_call(self):
        character = self.create_character()
        self.project.generation_settings = {
            "image_generation_model": "gemini-flash-image",
        }
        self.project.save(update_fields=["generation_settings"])
        CharacterGenerationJob.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            actor=self.user_key,
            job_type=GenerationJobType.INITIAL_VARIANTS,
            status=GenerationJobStatus.COMPLETED,
            variant_count=1,
            provider="gemini-flash-image",
            provider_started_at=timezone.now(),
        )
        provider = CountingProvider()

        with patch(PROVIDER_FACTORY, return_value=provider):
            with self.assertRaises(GenerationBudgetExceededError):
                CharacterGenerationService().create_initial_variants(
                    self.user_key,
                    self.project.id,
                    character.character_id,
                    {
                        "variant_count": 1,
                        "_idempotency_key": "daily-budget-test",
                    },
                )

        self.assertEqual(provider.calls, 0)

    @override_settings(
        CHARACTER_STUDIO_DAILY_BUDGET_PER_USER=1,
        CHARACTER_STUDIO_DAILY_BUDGET_PER_PROJECT=10,
    )
    def test_active_paid_job_reserves_remaining_user_budget(self):
        character = self.create_character()
        self.project.generation_settings = {
            "image_generation_model": "gemini-flash-image",
        }
        self.project.save(update_fields=["generation_settings"])
        CharacterGenerationJob.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            actor=self.user_key,
            job_type=GenerationJobType.INITIAL_VARIANTS,
            status=GenerationJobStatus.QUEUED,
            variant_count=1,
            provider="gemini-flash-image",
        )
        provider = CountingProvider()

        with patch(PROVIDER_FACTORY, return_value=provider):
            with self.assertRaises(GenerationBudgetExceededError):
                CharacterGenerationService().create_initial_variants(
                    self.user_key,
                    self.project.id,
                    character.character_id,
                    {
                        "variant_count": 1,
                        "_idempotency_key": "reserved-budget-test",
                    },
                )

        self.assertEqual(provider.calls, 0)

    @override_settings(
        CHARACTER_STUDIO_MAX_ACTIVE_GLOBAL=1,
        CHARACTER_STUDIO_MAX_ACTIVE_PER_PROJECT=1,
        CHARACTER_STUDIO_DAILY_BUDGET_PER_USER=1,
        CHARACTER_STUDIO_DAILY_BUDGET_PER_PROJECT=1,
    )
    def test_model3d_job_does_not_consume_image_generation_limits(self):
        character = self.create_character()
        self.project.generation_settings = {
            "image_generation_model": "gemini-flash-image",
        }
        self.project.save(update_fields=["generation_settings"])
        CharacterGenerationJob.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            actor=self.user_key,
            job_type=GenerationJobType.MODEL3D_RECONSTRUCTION,
            status=GenerationJobStatus.QUEUED,
            variant_count=1,
            provider="hunyuan3d-head-pipeline",
        )

        preview = build_generation_preview(
            actor=self.user_key,
            character=character,
            image_types=["portrait"],
        )
        self.assertEqual(preview["budgets"]["user"]["used"], 0)
        self.assertEqual(preview["budgets"]["project"]["used"], 0)
        self.assertEqual(preview["concurrency"]["global"]["active"], 0)
        self.assertEqual(preview["concurrency"]["project"]["active"], 0)
        provider = CountingProvider()

        with patch(PROVIDER_FACTORY, return_value=provider):
            job = CharacterGenerationService().create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {
                    "variant_count": 1,
                    "_idempotency_key": "model3d-does-not-block-images",
                },
            )

        self.assertEqual(job.status, GenerationJobStatus.COMPLETED)
        self.assertEqual(provider.calls, 1)

    def test_create_from_reference_replays_whole_endpoint(self):
        provider = CountingProvider()
        client = APIClient()
        url = f"/api/projects/{self.project.id}/characters/from-reference"
        headers = {
            "HTTP_X_USER_TOKEN": str(self.user_key.key),
            "HTTP_IDEMPOTENCY_KEY": "reference-create-42",
        }

        def form(name="Mira"):
            return {
                "name": name,
                "character_type": "human",
                "variants_count": "1",
                "reference_image": SimpleUploadedFile(
                    "reference.png",
                    MockProvider._PLACEHOLDER_PNG,
                    content_type="image/png",
                ),
            }

        with patch(PROVIDER_FACTORY, return_value=provider):
            first = client.post(url, form(), format="multipart", **headers)
            replay = client.post(url, form(), format="multipart", **headers)
            conflict = client.post(
                url,
                form(name="Different"),
                format="multipart",
                **headers,
            )

        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(replay.status_code, 202, replay.content)
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertEqual(
            first.json()["character"]["character_id"],
            replay.json()["character"]["character_id"],
        )
        self.assertEqual(
            first.json()["generation_job"]["job_id"],
            replay.json()["generation_job"]["job_id"],
        )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(
            StudioCharacter.objects.filter(project=self.project).count(),
            1,
        )
        character = StudioCharacter.objects.get(project=self.project)
        self.assertEqual(
            character.assets.filter(
                asset_type="uploaded_reference",
            ).count(),
            1,
        )
        for field in (
            "creation_idempotency_key",
            "creation_request_hash",
        ):
            self.assertNotIn(field, first.json()["character"])

    def test_project_generation_setting_uses_edit_settings_boundary(self):
        project_mutations.update_project_settings(
            actor=self.user_key.user,
            action=policy.Action.EDIT_SETTINGS,
            project_id=self.project.id,
            data={
                "generation_settings": {
                    "image_generation_model": "mock",
                }
            },
        )
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.generation_settings["image_generation_model"],
            "mock",
        )

        serializer = ProjectUpdateSerializer(
            data={
                "generation_settings": {
                    "image_generation_model": "unknown-provider",
                }
            }
        )
        self.assertFalse(serializer.is_valid())

    def test_actor_and_project_provider_are_not_character_creator(self):
        character = self.create_character()
        editor = User.objects.create_user(username="generation-editor")
        editor_key = UserKey.objects.create(user=editor)
        ProjectMember.objects.create(
            project=self.project,
            user=editor,
            role=ProjectMemberRole.EDITOR,
        )
        profile, _ = UserProfile.objects.get_or_create(user=editor)
        profile.image_generation_model = "gemini-flash-image"
        profile.save(update_fields=["image_generation_model"])
        self.project.generation_settings = {
            "image_generation_model": "mock",
        }
        self.project.save(update_fields=["generation_settings"])
        resolved_names = []

        def provider_factory(name):
            resolved_names.append(name)
            return MockProvider()

        with patch(PROVIDER_FACTORY, side_effect=provider_factory):
            job = CharacterGenerationService().create_initial_variants(
                editor_key,
                self.project.id,
                character.character_id,
                {
                    "variant_count": 1,
                    "provider": "gemini",
                },
            )

        self.assertEqual(resolved_names, ["mock"])
        self.assertEqual(job.actor, editor_key)
        self.assertEqual(job.user, self.user_key)
        self.assertEqual(job.variants.get().asset.user, editor_key)

    def test_actor_preference_is_fallback_without_project_setting(self):
        character = self.create_character()
        editor = User.objects.create_user(username="preference-editor")
        editor_key = UserKey.objects.create(user=editor)
        ProjectMember.objects.create(
            project=self.project,
            user=editor,
            role=ProjectMemberRole.EDITOR,
        )
        profile, _ = UserProfile.objects.get_or_create(user=editor)
        profile.image_generation_model = "gemini-flash-image"
        profile.save(update_fields=["image_generation_model"])
        self.project.generation_settings = {}
        self.project.save(update_fields=["generation_settings"])
        resolved_names = []

        def provider_factory(name):
            resolved_names.append(name)
            return MockProvider()

        with patch(PROVIDER_FACTORY, side_effect=provider_factory):
            CharacterGenerationService().create_initial_variants(
                editor_key,
                self.project.id,
                character.character_id,
                {"variant_count": 1},
            )

        self.assertEqual(resolved_names, ["gemini-flash-image"])

    def test_stale_recovery_requeues_only_before_provider_start(self):
        character = self.create_character()
        common = {
            "character": character,
            "project": self.project,
            "user": self.user_key,
            "actor": self.user_key,
            "job_type": GenerationJobType.INITIAL_VARIANTS,
            "status": GenerationJobStatus.PROCESSING,
            "variant_count": 1,
            "attempts": 1,
            "max_attempts": 3,
            "lease_token": uuid.uuid4(),
            "lease_expires_at": timezone.now() - timedelta(seconds=1),
        }
        safe_job = CharacterGenerationJob.objects.create(**common)
        ambiguous_job = CharacterGenerationJob.objects.create(
            **{
                **common,
                "lease_token": uuid.uuid4(),
                "provider_started_at": timezone.now() - timedelta(minutes=1),
            }
        )

        result = recover_stale_jobs()
        safe_job.refresh_from_db()
        ambiguous_job.refresh_from_db()

        self.assertEqual(safe_job.status, GenerationJobStatus.QUEUED)
        self.assertEqual(
            ambiguous_job.status,
            GenerationJobStatus.FAILED,
        )
        self.assertEqual(
            ambiguous_job.error_code,
            "PROVIDER_OUTCOME_UNKNOWN",
        )
        self.assertIn(str(safe_job.job_id), result["requeued"])
        self.assertIn(str(ambiguous_job.job_id), result["failed"])

    def test_unknown_provider_outcome_requires_explicit_key_for_retry(self):
        character = self.create_character()
        service = CharacterGenerationService()
        params = {"variant_count": 1}
        ambiguous_job = service.create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            params,
        )
        ambiguous_job.status = GenerationJobStatus.FAILED
        ambiguous_job.error_code = "PROVIDER_OUTCOME_UNKNOWN"
        ambiguous_job.error_message = "Provider result cannot be determined."
        ambiguous_job.failed_at = timezone.now()
        ambiguous_job.save()
        provider = CountingProvider()

        with patch(PROVIDER_FACTORY, return_value=provider):
            replay = service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                params,
            )
            explicit_retry = service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {
                    **params,
                    "_idempotency_key": "explicit-retry-after-unknown",
                },
            )

        self.assertEqual(replay.job_id, ambiguous_job.job_id)
        self.assertEqual(replay.status, GenerationJobStatus.FAILED)
        self.assertEqual(explicit_retry.status, GenerationJobStatus.COMPLETED)
        self.assertEqual(provider.calls, 1)

    def test_request_hash_includes_compiled_character_state(self):
        character = self.create_character()
        service = CharacterGenerationService()
        provider = CountingProvider()
        params = {
            "variant_count": 1,
            "_idempotency_key": "compiled-state-42",
        }

        with patch(PROVIDER_FACTORY, return_value=provider):
            service.create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                params,
            )
            character.active_appearance.hair_color = "blue"
            character.active_appearance.save(update_fields=["hair_color"])
            with self.assertRaises(ValidationError):
                service.create_initial_variants(
                    self.user_key,
                    self.project.id,
                    character.character_id,
                    params,
                )

        self.assertEqual(provider.calls, 1)

    def test_unknown_provider_fails_job_instead_of_using_mock(self):
        character = self.create_character()
        self.project.generation_settings = {
            "image_generation_model": "stale-provider-key",
        }
        self.project.save(update_fields=["generation_settings"])

        job = CharacterGenerationService().create_initial_variants(
            self.user_key,
            self.project.id,
            character.character_id,
            {"variant_count": 1},
        )

        self.assertEqual(job.status, GenerationJobStatus.FAILED)
        self.assertEqual(job.error_code, "PROVIDER_CONFIGURATION_ERROR")
        self.assertEqual(job.variants.count(), 0)

    def test_recovery_command_executes_queued_crash_gap_job(self):
        character = self.create_character()
        job = CharacterGenerationJob.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            actor=self.user_key,
            job_type=GenerationJobType.INITIAL_VARIANTS,
            status=GenerationJobStatus.QUEUED,
            variant_count=1,
            request_payload={"image_type": "portrait"},
            compiled_prompt="portrait",
            negative_prompt="",
            compiled_metadata={"image_type": "portrait"},
            attempts=1,
            max_attempts=3,
        )

        call_command(
            "recover_character_generation_jobs",
            limit=10,
            stdout=StringIO(),
        )

        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJobStatus.COMPLETED)
        self.assertEqual(job.variants.count(), 1)

    def test_viewer_model3d_get_has_no_generation_side_effect(self):
        character = self.create_character()
        character.status = "references_locked"
        character.save(update_fields=["status", "updated_at"])
        viewer = User.objects.create_user(username="model3d-viewer")
        viewer_key = UserKey.objects.create(user=viewer)
        ProjectMember.objects.create(
            project=self.project,
            user=viewer,
            role=ProjectMemberRole.VIEWER,
        )
        client = APIClient()

        response = client.get(
            (
                f"/api/projects/{self.project.id}/characters/"
                f"{character.character_id}/model3d"
            ),
            HTTP_X_USER_TOKEN=str(viewer_key.key),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(
            CharacterGenerationJob.objects.filter(
                character=character,
                job_type=GenerationJobType.MODEL3D_RECONSTRUCTION,
            ).exists()
        )

    def test_terminal_update_is_fenced_by_lease_token(self):
        character = self.create_character()
        job = CharacterGenerationJob.objects.create(
            character=character,
            project=self.project,
            user=self.user_key,
            actor=self.user_key,
            job_type=GenerationJobType.INITIAL_VARIANTS,
            variant_count=1,
        )
        lease = claim_job(job.job_id)
        wrong_lease = JobLease(
            job_id=job.job_id,
            token=uuid.uuid4(),
            timeout_seconds=lease.timeout_seconds,
        )

        fail_job(
            wrong_lease,
            error_code="STALE_WORKER",
            error_message="must not win",
        )
        job.refresh_from_db()
        self.assertEqual(job.status, GenerationJobStatus.PROCESSING)
        self.assertEqual(job.lease_token, lease.token)
