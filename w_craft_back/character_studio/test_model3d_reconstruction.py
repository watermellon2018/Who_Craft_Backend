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
    SIDE_REFERENCE_TYPES,
    _execute_pipeline,
    _selected_references,
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
        self.reference_assets = {}
        for asset_type in (
            CharacterAssetType.PORTRAIT,
            CharacterAssetType.FULL_BODY,
            CharacterAssetType.PROFILE,
            CharacterAssetType.BACK_VIEW,
        ):
            asset = CharacterAsset.objects.create(
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
            self.reference_assets[asset_type] = asset

    def _ensure_and_dispatch(self):
        with patch(DISPATCH) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                state = ensure_reconstruction(self.character)
        return state, dispatch

    def _add_reference(self, asset_type: str) -> CharacterAsset:
        asset = CharacterAsset.objects.create(
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
        self.reference_assets[asset_type] = asset
        return asset

    def _capture_pipeline_commands(
        self,
        reference_contents: dict[str, bytes] | None = None,
    ) -> tuple[list[list[str]], dict]:
        state, _ = self._ensure_and_dispatch()
        job = CharacterGenerationJob.objects.get(job_id=state["job_id"])
        references = _selected_references(self.character)
        commands: list[list[str]] = []
        reference_contents = reference_contents or {}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            tools_root = root / "tools"
            tools_root.mkdir()
            for script_name in (
                "prepare_hunyuan_views.py",
                "run_hunyuan_multiview.py",
                "postprocess_hunyuan_mesh.py",
            ):
                (tools_root / script_name).write_text("", encoding="utf-8")

            for asset_type in (CharacterAssetType.PORTRAIT, *SIDE_REFERENCE_TYPES):
                asset = references.get(asset_type)
                if asset is None:
                    continue
                path = media_root / asset.storage_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(
                    reference_contents.get(
                        asset_type,
                        f"unique-{asset_type}".encode("utf-8"),
                    )
                )

            def capture(command: list[str], _cwd: Path) -> None:
                commands.append(command)

            with override_settings(
                MEDIA_ROOT=media_root,
                MODEL3D_RECONSTRUCTION_PYTHON="python",
                MODEL3D_RECONSTRUCTION_TOOLS_ROOT=tools_root,
                MODEL3D_HUNYUAN_ROOT=root / "hunyuan",
                MODEL3D_MODEL_ROOT=root / "models",
            ):
                metadata = _execute_pipeline(
                    job,
                    references,
                    root / "head.glb",
                    root / "work",
                    capture,
                )
        return commands, metadata

    @staticmethod
    def _view_args(command: list[str]) -> list[tuple[str, str]]:
        result = []
        for index, argument in enumerate(command[:-1]):
            if argument in ("--front", "--left", "--right"):
                result.append((argument, Path(command[index + 1]).name))
        return result

    @staticmethod
    def _argument_value(command: list[str], name: str) -> str:
        return command[command.index(name) + 1]

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

    def test_pipeline_passes_profile_and_three_quarter_in_order(self):
        self._add_reference(CharacterAssetType.THREE_QUARTER)

        selected = _selected_references(self.character)
        self.assertEqual(
            list(selected),
            [
                CharacterAssetType.PORTRAIT,
                CharacterAssetType.FULL_BODY,
                CharacterAssetType.BACK_VIEW,
                CharacterAssetType.PROFILE,
                CharacterAssetType.THREE_QUARTER,
            ],
        )

        commands, metadata = self._capture_pipeline_commands()

        expected_source_views = [
            ("--front", "portrait.png"),
            ("--left", "profile.png"),
            ("--right", "three_quarter.png"),
        ]
        expected_prepared_views = [
            ("--front", "front.png"),
            ("--left", "left.png"),
            ("--right", "right.png"),
        ]
        self.assertEqual(self._view_args(commands[0]), expected_source_views)
        self.assertEqual(self._view_args(commands[1]), expected_prepared_views)
        self.assertEqual(
            self._argument_value(commands[0], "--left-reference-type"),
            CharacterAssetType.PROFILE,
        )
        self.assertEqual(
            self._argument_value(commands[0], "--right-reference-type"),
            CharacterAssetType.THREE_QUARTER,
        )
        self.assertEqual(
            [item["cardinal_slot_is_approximation"] for item in metadata["reference_views"]],
            [False, True, True],
        )

        self.assertEqual(
            [item["reference_type"] for item in metadata["reference_views"]],
            [
                CharacterAssetType.PORTRAIT,
                CharacterAssetType.PROFILE,
                CharacterAssetType.THREE_QUARTER,
            ],
        )
        job = CharacterGenerationJob.objects.latest("created_at")
        self.assertEqual(job.request_payload["pipeline_version"], 3)
        self.assertSetEqual(
            set(job.request_payload["reference_asset_ids"]),
            {
                CharacterAssetType.PORTRAIT,
                CharacterAssetType.FULL_BODY,
                CharacterAssetType.BACK_VIEW,
                CharacterAssetType.PROFILE,
                CharacterAssetType.THREE_QUARTER,
            },
        )

    def test_pipeline_keeps_legacy_front_and_profile_arguments(self):
        commands, metadata = self._capture_pipeline_commands()

        expected_source_views = [
            ("--front", "portrait.png"),
            ("--left", "profile.png"),
        ]
        expected_prepared_views = [
            ("--front", "front.png"),
            ("--left", "left.png"),
        ]
        self.assertEqual(self._view_args(commands[0]), expected_source_views)
        self.assertEqual(self._view_args(commands[1]), expected_prepared_views)
        self.assertEqual(metadata["skipped_duplicate_reference_types"], [])

    def test_pipeline_skips_duplicate_three_quarter_content(self):
        self._add_reference(CharacterAssetType.THREE_QUARTER)
        commands, metadata = self._capture_pipeline_commands(
            {
                CharacterAssetType.PROFILE: b"same-side",
                CharacterAssetType.THREE_QUARTER: b"same-side",
            }
        )

        expected_source_views = [
            ("--front", "portrait.png"),
            ("--left", "profile.png"),
        ]
        expected_prepared_views = [
            ("--front", "front.png"),
            ("--left", "left.png"),
        ]
        self.assertEqual(self._view_args(commands[0]), expected_source_views)
        self.assertEqual(self._view_args(commands[1]), expected_prepared_views)
        self.assertEqual(
            metadata["skipped_duplicate_reference_types"],
            [CharacterAssetType.THREE_QUARTER],
        )

    def test_three_quarter_falls_back_to_left_when_profile_is_missing(self):
        self.reference_assets.pop(CharacterAssetType.PROFILE).delete()
        self._add_reference(CharacterAssetType.THREE_QUARTER)

        commands, _ = self._capture_pipeline_commands()

        expected_source_views = [
            ("--front", "portrait.png"),
            ("--left", "three_quarter.png"),
        ]
        expected_prepared_views = [
            ("--front", "front.png"),
            ("--left", "left.png"),
        ]
        self.assertEqual(self._view_args(commands[0]), expected_source_views)
        self.assertEqual(self._view_args(commands[1]), expected_prepared_views)
        self.assertEqual(
            self._argument_value(commands[0], "--left-reference-type"),
            CharacterAssetType.THREE_QUARTER,
        )

    def test_missing_all_side_references_does_not_create_job(self):
        self.reference_assets.pop(CharacterAssetType.PROFILE).delete()

        state, dispatch = self._ensure_and_dispatch()

        self.assertEqual(state["status"], "missing")
        dispatch.assert_not_called()

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
