"""Tests for per-character 3D reconstruction orchestration."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    CharacterGenerationJob,
    CharacterStatus,
    GenerationJobStatus,
    GenerationJobType,
)
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.model3d_reconstruction_service import (
    ensure_reconstruction,
    reconstruction_state,
    retry_reconstruction,
    run_reconstruction_job,
)
from w_craft_back.movie.project.models import Project


DISPATCH = (
    "w_craft_back.character_studio.services.model3d_reconstruction_service."
    "dispatch_reconstruction"
)
PIPELINE = (
    "w_craft_back.character_studio.services.model3d_reconstruction_service."
    "_execute_pipeline"
)


class Model3DReconstructionTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="model3d-owner", password="x")
        self.user_key = UserKey.objects.create(user=user)
        self.project = Project.objects.create(
            user=self.user_key,
            title="3D film",
            format="series",
            annot="Short",
            desc="Long",
        )
        self.character = CharacterService().create_character(
            self.user_key,
            self.project,
            {
                "name": "Mira",
                "gender": "girl",
                "role": "main",
                "appearance_description": "copper hair and blue eyes",
            },
        )
        self.character.status = CharacterStatus.REFERENCES_LOCKED
        self.character.save(update_fields=("status", "updated_at"))
        for asset_type in (
            CharacterAssetType.PORTRAIT,
            CharacterAssetType.FULL_BODY,
            CharacterAssetType.PROFILE,
            CharacterAssetType.BACK_VIEW,
        ):
            CharacterAsset.objects.create(
                character=self.character,
                project=self.project,
                user=self.user_key,
                asset_type=asset_type,
                image_url=f"/media/{asset_type}.png",
                storage_path=f"tests/{asset_type}.png",
                mime_type="image/png",
                source="test",
                status=CharacterAssetStatus.READY,
                metadata={"sha256": f"hash-{asset_type}"},
            )

    def _ensure_and_dispatch(self):
        with patch(DISPATCH) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                state = ensure_reconstruction(self.character)
        return state, dispatch

    def test_ensure_creates_one_idempotent_job_and_asset(self):
        state, dispatch = self._ensure_and_dispatch()
        self.assertEqual(state["status"], "queued")
        self.assertEqual(state["progress"], 0)
        dispatch.assert_called_once()
        self.assertEqual(
            CharacterGenerationJob.objects.filter(
                job_type=GenerationJobType.MODEL3D_RECONSTRUCTION,
            ).count(),
            1,
        )
        asset = CharacterAsset.objects.get(asset_type=CharacterAssetType.MODEL_3D)
        self.assertEqual(asset.status, CharacterAssetStatus.GENERATING)
        self.assertEqual(str(asset.source_job_id), state["job_id"])

        with patch(DISPATCH) as second_dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                second = ensure_reconstruction(self.character)
        self.assertEqual(second["job_id"], state["job_id"])
        second_dispatch.assert_not_called()

    def test_worker_success_publishes_personal_glb(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(
                MEDIA_ROOT=media_root,
                MEDIA_URL="/media/",
                PUBLIC_BASE_URL="http://testserver",
            ):
                state, _ = self._ensure_and_dispatch()

                def fake_pipeline(job, references, output_path, work_dir, runner):
                    self.assertIn(CharacterAssetType.PORTRAIT, references)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(b"glTF-personal-head")
                    return {"test_pipeline": True}

                with patch(PIPELINE, side_effect=fake_pipeline):
                    asset = run_reconstruction_job(state["job_id"])

                self.assertIsNotNone(asset)
                job = CharacterGenerationJob.objects.get(job_id=state["job_id"])
                self.assertEqual(job.status, GenerationJobStatus.COMPLETED)
                self.assertEqual(job.progress, 100)
                ready = reconstruction_state(self.character)
                self.assertEqual(ready["status"], "ready")
                self.assertEqual(
                    ready["model_url"],
                    f"http://testserver/media/{asset.storage_path}",
                )
                self.assertTrue((Path(media_root) / asset.storage_path).is_file())

    def test_worker_failure_is_visible_and_retry_creates_new_job(self):
        state, _ = self._ensure_and_dispatch()
        with patch(PIPELINE, side_effect=RuntimeError("CUDA unavailable")):
            result = run_reconstruction_job(state["job_id"])
        self.assertIsNone(result)
        failed = reconstruction_state(self.character)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("CUDA unavailable", failed["error_message"])

        with patch(DISPATCH) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                retried = retry_reconstruction(self.character)
        self.assertEqual(retried["status"], "queued")
        self.assertNotEqual(retried["job_id"], state["job_id"])
        dispatch.assert_called_once()

    def test_unlocked_character_does_not_auto_create_from_state(self):
        self.character.status = CharacterStatus.ACTIVE
        self.character.save(update_fields=("status", "updated_at"))
        state = reconstruction_state(self.character, ensure=True)
        self.assertEqual(state["status"], "missing")
        self.assertFalse(
            CharacterGenerationJob.objects.filter(
                job_type=GenerationJobType.MODEL3D_RECONSTRUCTION,
            ).exists()
        )
