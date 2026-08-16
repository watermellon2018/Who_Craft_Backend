"""Tests for per-character 3D reconstruction orchestration."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from PIL import Image, ImageDraw

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
from w_craft_back.character_studio.services.errors import ValidationError
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
            owner=user,
            title="3D film",
            format="series",
            annotation="Short",
            synopsis="Long",
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
            CharacterAssetType.THREE_QUARTER,
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

            for asset_type in (
                CharacterAssetType.PORTRAIT,
                CharacterAssetType.BACK_VIEW,
                *SIDE_REFERENCE_TYPES,
            ):
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
            if argument in ("--front", "--left", "--back", "--right"):
                result.append((argument, Path(command[index + 1]).name))
        return result

    @staticmethod
    def _argument_value(command: list[str], name: str) -> str:
        return command[command.index(name) + 1]

    @staticmethod
    def _write_reference_png(path: Path, color: tuple[int, int, int]) -> None:
        """Write a small face-like fixture without using the prepare runtime."""
        image = Image.new("RGB", (256, 256), (220, 220, 220))
        drawing = ImageDraw.Draw(image)
        drawing.ellipse((72, 28, 184, 206), fill=color)
        drawing.rectangle((104, 168, 152, 244), fill=color)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")

    def test_ensure_creates_one_idempotent_job_and_asset(self):
        state, dispatch = self._ensure_and_dispatch()
        self.assertEqual(state["status"], "queued")
        self.assertEqual(state["progress"], 0)
        dispatch.assert_not_called()
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

    def test_views_from_an_old_portrait_block_reconstruction(self):
        for asset_type in (
            CharacterAssetType.FULL_BODY,
            CharacterAssetType.PROFILE,
            CharacterAssetType.THREE_QUARTER,
            CharacterAssetType.BACK_VIEW,
        ):
            asset = self.reference_assets[asset_type]
            asset.metadata = {
                **asset.metadata,
                "source_identity_asset_id": "replaced-portrait-id",
            }
            asset.save(update_fields=("metadata", "updated_at"))

        state, dispatch = self._ensure_and_dispatch()

        self.assertEqual(state["status"], "missing")
        self.assertIn("different identity", state["error_message"])
        dispatch.assert_not_called()

    def test_shared_identity_anchor_survives_portrait_regeneration(self):
        identity_source_id = "canonical-identity-asset"
        for asset in self.reference_assets.values():
            asset.metadata = {
                **asset.metadata,
                "source_identity_asset_id": identity_source_id,
            }
            asset.save(update_fields=("metadata", "updated_at"))

        state, dispatch = self._ensure_and_dispatch()

        self.assertEqual(state["status"], "queued")
        self.assertIsNotNone(state["job_id"])
        dispatch.assert_not_called()
        self.assertTrue(
            CharacterGenerationJob.objects.filter(
                job_id=state["job_id"],
            ).exists()
        )

    def test_pipeline_passes_profile_and_three_quarter_in_order(self):

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
            ("--back", "back_view.png"),
            ("--right", "three_quarter.png"),
        ]
        expected_prepared_views = [
            ("--front", "front.png"),
            ("--left", "left.png"),
            ("--back", "back.png"),
            ("--right", "right.png"),
        ]
        self.assertEqual(self._view_args(commands[0]), expected_source_views)
        self.assertEqual(self._view_args(commands[1]), expected_prepared_views)
        self.assertEqual(
            Path(self._argument_value(commands[2], "--front-reference")).name,
            "front.png",
        )
        self.assertEqual(
            Path(self._argument_value(commands[2], "--profile-reference")).name,
            "left.png",
        )
        self.assertEqual(
            self._argument_value(commands[2], "--hair-color"),
            "#1e1a18",
        )
        self.assertEqual(
            self._argument_value(commands[2], "--skin-color"),
            "#d8ab8a",
        )
        self.assertEqual(
            self._argument_value(commands[0], "--left-reference-type"),
            CharacterAssetType.PROFILE,
        )
        self.assertEqual(
            self._argument_value(commands[0], "--right-reference-type"),
            CharacterAssetType.THREE_QUARTER,
        )
        self.assertEqual(
            [
                item["cardinal_slot_is_approximation"]
                for item in metadata["reference_views"]
            ],
            [False, True, False, True],
        )

        self.assertEqual(
            [item["reference_type"] for item in metadata["reference_views"]],
            [
                CharacterAssetType.PORTRAIT,
                CharacterAssetType.PROFILE,
                CharacterAssetType.BACK_VIEW,
                CharacterAssetType.THREE_QUARTER,
            ],
        )
        job = CharacterGenerationJob.objects.latest("created_at")
        self.assertEqual(job.request_payload["pipeline_version"], 5)
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

    def test_missing_three_quarter_reference_does_not_create_job(self):
        self.reference_assets.pop(CharacterAssetType.THREE_QUARTER).delete()

        state, dispatch = self._ensure_and_dispatch()

        self.assertEqual(state["status"], "missing")
        self.assertIn("Required references", state["error_message"])
        dispatch.assert_not_called()

    def test_duplicate_side_views_block_four_view_reconstruction(self):
        with self.assertRaisesRegex(ValidationError, "Four unique"):
            self._capture_pipeline_commands(
                {
                    CharacterAssetType.PROFILE: b"same-side",
                    CharacterAssetType.THREE_QUARTER: b"same-side",
                }
            )

    def test_missing_profile_reference_does_not_create_job(self):
        self.reference_assets.pop(CharacterAssetType.PROFILE).delete()

        state, dispatch = self._ensure_and_dispatch()

        self.assertEqual(state["status"], "missing")
        self.assertIn("Required references", state["error_message"])
        dispatch.assert_not_called()

    def test_cross_repo_prepare_cli_smoke(self):
        if os.environ.get("W_CRAFT_RUN_CROSS_REPO_SMOKE") != "1":
            self.skipTest("set W_CRAFT_RUN_CROSS_REPO_SMOKE=1 to run")

        state, _ = self._ensure_and_dispatch()
        job = CharacterGenerationJob.objects.get(job_id=state["job_id"])
        references = _selected_references(self.character)

        backend_root = Path(__file__).resolve().parents[2]
        default_tools_root = (
            backend_root.parent / "who_craft" / "tools" / "reconstruction"
        )
        tools_root = Path(
            os.environ.get(
                "W_CRAFT_CROSS_REPO_TOOLS_ROOT",
                default_tools_root,
            )
        )
        prepare_script = tools_root / "prepare_hunyuan_views.py"
        default_conda_exe = Path.home() / "miniconda3" / "Scripts" / "conda.exe"
        conda_exe = Path(
            os.environ.get("W_CRAFT_CROSS_REPO_CONDA_EXE", default_conda_exe)
        )
        conda_environment = os.environ.get("W_CRAFT_CROSS_REPO_CONDA_ENV", "basic")
        self.assertTrue(
            prepare_script.is_file(),
            f"missing sibling tool: {prepare_script}",
        )
        self.assertTrue(conda_exe.is_file(), f"missing conda executable: {conda_exe}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            source_paths = {
                CharacterAssetType.PORTRAIT: media_root / "tests" / "portrait.png",
                CharacterAssetType.PROFILE: media_root / "tests" / "profile.png",
                CharacterAssetType.THREE_QUARTER: (
                    media_root / "tests" / "three_quarter.png"
                ),
                CharacterAssetType.BACK_VIEW: media_root / "tests" / "back_view.png",
            }
            colors = {
                CharacterAssetType.PORTRAIT: (174, 110, 72),
                CharacterAssetType.PROFILE: (166, 104, 68),
                CharacterAssetType.THREE_QUARTER: (158, 98, 64),
                CharacterAssetType.BACK_VIEW: (150, 92, 60),
            }
            for asset_type, source_path in source_paths.items():
                self._write_reference_png(source_path, colors[asset_type])

            commands: list[list[str]] = []
            executed_scripts: list[str] = []

            def run_prepare_only(command: list[str], cwd: Path) -> None:
                commands.append(command)
                if str(prepare_script) in command:
                    subprocess.run(
                        command,
                        cwd=str(cwd),
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    executed_scripts.append(prepare_script.name)

            work_dir = root / "work"
            with override_settings(
                MEDIA_ROOT=media_root,
                MODEL3D_RECONSTRUCTION_PYTHON="",
                MODEL3D_CONDA_EXE=conda_exe,
                MODEL3D_CONDA_ENV=conda_environment,
                MODEL3D_RECONSTRUCTION_TOOLS_ROOT=tools_root,
                MODEL3D_HUNYUAN_ROOT=root / "unused-hunyuan",
                MODEL3D_MODEL_ROOT=root / "unused-models",
            ):
                pipeline_metadata = _execute_pipeline(
                    job,
                    references,
                    root / "unused-head.glb",
                    work_dir,
                    run_prepare_only,
                )

            script_names = [
                next(
                    Path(argument).name
                    for argument in command
                    if argument.endswith(".py")
                )
                for command in commands
            ]
            self.assertEqual(
                script_names,
                [
                    "prepare_hunyuan_views.py",
                    "run_hunyuan_multiview.py",
                    "postprocess_hunyuan_mesh.py",
                ],
            )
            self.assertEqual(executed_scripts, ["prepare_hunyuan_views.py"])
            self.assertEqual(
                self._view_args(commands[0]),
                [
                    ("--front", "portrait.png"),
                    ("--left", "profile.png"),
                    ("--back", "back_view.png"),
                    ("--right", "three_quarter.png"),
                ],
            )
            self.assertEqual(
                self._view_args(commands[1]),
                [
                    ("--front", "front.png"),
                    ("--left", "left.png"),
                    ("--back", "back.png"),
                    ("--right", "right.png"),
                ],
            )

            prepared_dir = work_dir / "prepared"
            inputs_dir = prepared_dir / "inputs"
            self.assertEqual(
                sorted(path.name for path in inputs_dir.iterdir()),
                ["back.png", "front.png", "left.png", "right.png"],
            )
            report_path = prepared_dir / "input-metadata.json"
            with report_path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)

            self.assertEqual(list(report["views"]), ["front", "left", "back", "right"])
            self.assertEqual(report["skipped_duplicate_views"], [])
            expected_sources = {
                "front": source_paths[CharacterAssetType.PORTRAIT],
                "left": source_paths[CharacterAssetType.PROFILE],
                "back": source_paths[CharacterAssetType.BACK_VIEW],
                "right": source_paths[CharacterAssetType.THREE_QUARTER],
            }
            for view_name, source_path in expected_sources.items():
                output_path = inputs_dir / f"{view_name}.png"
                self.assertEqual(
                    Path(report["views"][view_name]["source"]),
                    source_path.resolve(),
                )
                self.assertEqual(
                    Path(report["views"][view_name]["output"]),
                    output_path,
                )
                with Image.open(output_path) as image:
                    self.assertEqual(image.mode, "RGBA")
                    self.assertEqual(image.size, (512, 512))

            self.assertEqual(
                [
                    item["reference_type"]
                    for item in pipeline_metadata["reference_views"]
                ],
                [
                    CharacterAssetType.PORTRAIT,
                    CharacterAssetType.PROFILE,
                    CharacterAssetType.BACK_VIEW,
                    CharacterAssetType.THREE_QUARTER,
                ],
            )
            self.assertFalse((work_dir / "hunyuan").exists())

    def test_missing_back_reference_does_not_create_job(self):
        self.reference_assets.pop(CharacterAssetType.BACK_VIEW).delete()

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
                    output_path.with_name("hair.glb").write_bytes(b"glTF-personal-hair")
                    return {"test_pipeline": True}

                with patch(PIPELINE, side_effect=fake_pipeline):
                    asset = run_reconstruction_job(state["job_id"])

                self.assertIsNotNone(asset)
                job = CharacterGenerationJob.objects.get(job_id=state["job_id"])
                self.assertEqual(job.status, GenerationJobStatus.COMPLETED)
                self.assertEqual(job.progress, 100)
                ready = reconstruction_state(self.character)
                self.assertEqual(ready["status"], "ready")
                self.assertTrue(
                    urlparse(ready["model_url"]).path.startswith("/api/media/")
                )
                self.assertTrue(
                    urlparse(ready["hair_url"]).path.startswith("/api/media/")
                )
                self.assertEqual(ready["assets"]["hair"]["source"], "generated")
                self.assertEqual(
                    ready["assets"]["hair"]["generation_method"],
                    "multiview_hunyuan_voxel_remesh_with_inset_backing_v3",
                )
                self.assertTrue((Path(media_root) / asset.storage_path).is_file())

    def test_retry_replaces_ready_asset_missing_generated_hair(self):
        state, _ = self._ensure_and_dispatch()
        job = CharacterGenerationJob.objects.get(job_id=state["job_id"])
        job.status = GenerationJobStatus.COMPLETED
        job.progress = 100
        job.save(update_fields=("status", "progress"))
        asset = CharacterAsset.objects.get(source_job_id=job.job_id)
        asset.status = CharacterAssetStatus.READY
        asset.metadata = {
            key: value
            for key, value in asset.metadata.items()
            if key != "hair_asset"
        }
        asset.save(update_fields=("status", "metadata", "updated_at"))

        failed = reconstruction_state(self.character)
        self.assertEqual(failed["status"], "failed")

        with patch(DISPATCH) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                retried = retry_reconstruction(self.character)

        self.assertEqual(retried["status"], "queued")
        self.assertNotEqual(retried["job_id"], state["job_id"])
        dispatch.assert_not_called()

    def test_worker_failure_is_safe_and_retry_creates_new_job(self):
        private_fragment = "log01-private-provider-fragment-3d"
        state, _ = self._ensure_and_dispatch()
        with self.assertLogs(
            "w_craft_back.character_studio.services.model3d_reconstruction_service",
            level="ERROR",
        ) as captured, patch(
            PIPELINE,
            side_effect=RuntimeError(private_fragment),
        ):
            result = run_reconstruction_job(state["job_id"])
        self.assertIsNone(result)
        failed = reconstruction_state(self.character)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            failed["error_message"],
            "Reconstruction failed. Try again.",
        )
        self.assertNotIn(private_fragment, "\n".join(captured.output))
        self.assertNotIn(private_fragment, failed["error_message"])

        with patch(DISPATCH) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                retried = retry_reconstruction(self.character)
        self.assertEqual(retried["status"], "queued")
        self.assertNotEqual(retried["job_id"], state["job_id"])
        dispatch.assert_not_called()

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
