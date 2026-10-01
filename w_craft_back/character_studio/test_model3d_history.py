"""Real database/API regressions for durable 3D selection and revision restore."""

from copy import deepcopy
import hashlib
from io import StringIO
import json
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlparse

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as ModelValidationError
from django.core.management import call_command
from django.test import override_settings
from django.urls import resolve
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterAsset, CharacterAssetStatus, CharacterAssetType,
    CharacterGenerationJob, GenerationJobStatus, GenerationJobType,
    RevisionChangeType,
)
from w_craft_back.character_studio.services.errors import (
    PermissionDeniedError, ValidationError,
)
from w_craft_back.character_studio.services.model3d_history_service import (
    model3d_snapshot,
)
from w_craft_back.character_studio.services.model3d_publish_service import (
    publish_reviewed_model3d,
)
from w_craft_back.character_studio.services.model3d_reconstruction_service import (
    _reference_fingerprint, _selected_references, ensure_reconstruction,
    reconstruction_state, retry_reconstruction, run_reconstruction_job,
)
from w_craft_back.character_studio.services.revision_service import (
    CharacterRevisionService,
)
from w_craft_back.character_studio.services.serialization import character_dict
from w_craft_back.character_studio.tests import CharacterStudioTestCase
from w_craft_back.movie.project.policy import Action


class Model3DHistoryTests(CharacterStudioTestCase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.character = self.create_character()
        self.character.status = "references_locked"
        self.character.save(update_fields=["status"])
        self.client = APIClient()
        self.client.credentials(HTTP_X_USER_TOKEN=str(self.user_key.key))
        self.revisions = CharacterRevisionService()
        for asset_type in (
            "portrait", "full_body", "profile", "three_quarter", "back_view",
        ):
            CharacterAsset.objects.create(
                character=self.character, project=self.project, user=self.user_key,
                asset_type=asset_type, status=CharacterAssetStatus.READY,
                image_url=f"/media/{asset_type}.png", storage_path=f"{asset_type}.png",
            )

    def _asset(self):
        fingerprint = _reference_fingerprint(_selected_references(self.character))
        job = CharacterGenerationJob.objects.create(
            character=self.character, project=self.project, user=self.user_key,
            job_type=GenerationJobType.MODEL3D_RECONSTRUCTION,
            status=GenerationJobStatus.COMPLETED,
            request_payload={"reference_fingerprint": fingerprint},
        )
        prefix = f"test-model3d/{job.pk}"
        metadata = {"reference_fingerprint": fingerprint}
        for name in ("head", "hair"):
            key = f"{prefix}/{name}.glb"
            data = f"test-{job.pk}-{name}".encode()
            path = Path(self.media.name) / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            component = {"sha256": hashlib.sha256(data).hexdigest()}
            if name == "head":
                metadata.update(component)
            else:
                metadata["hair_asset"] = {
                    **component, "storage_path": key, "model_url": f"/media/{key}",
                }
        return CharacterAsset.objects.create(
            character=self.character, project=self.project, user=self.user_key,
            asset_type=CharacterAssetType.MODEL_3D, status=CharacterAssetStatus.READY,
            source_job_id=job.pk, image_url=f"/media/{prefix}/head.glb",
            storage_path=f"{prefix}/head.glb", metadata=metadata,
        )

    def _revision(self, asset=None, params=None):
        self.character.model3d_params = params or {"eyes": {"eyeSize": 0.3}}
        self.character.model3d_autofit_done = True
        self.character.model3d_autofit_version = 7
        self.character.active_model3d_asset = asset
        self.character.save()
        revision = self.revisions.create_revision(
            self.user_key, Action.EDIT_CONTENT, self.character,
            RevisionChangeType.MANUAL_UPDATE,
            snapshot={"stage": "3d_model"},
        )
        self.character.refresh_from_db()
        return revision

    def _restore(self, revision):
        return self.revisions.restore_revision(
            self.user_key, Action.EDIT_CONTENT, self.character, revision,
        )

    def _url(self):
        return f"/api/projects/{self.project.pk}/characters/{self.character.pk}/model3d"

    def test_custom_snapshot_contains_complete_state_without_signed_urls(self):
        asset = self._asset()
        revision = self._revision(asset)
        snapshot = revision.after_snapshot
        self.assertEqual(snapshot["stage"], "3d_model")
        self.assertEqual(snapshot["model3d_asset_id"], str(asset.pk))
        self.assertEqual(snapshot["model3d_autofit_version"], 7)
        self.assertEqual(snapshot["model3d_params"], self.character.model3d_params)
        self.assertNotIn("model_url", str(model3d_snapshot(self.character)))
        self.assertNotIn("active_model3d_asset", character_dict(self.character))

    def test_restore_selects_original_asset_and_params_over_newest(self):
        original = self._asset()
        first = self._revision(original)
        newer = self._asset()
        self._revision(newer, {"eyes": {"eyeSize": -0.7}})
        self.character.model3d_autofit_version = 6
        self.character.save()
        restored = self._restore(first)
        self.character.refresh_from_db()
        self.assertEqual(self.character.active_model3d_asset_id, original.pk)
        self.assertEqual(
            self.character.model3d_params, first.after_snapshot["model3d_params"],
        )
        self.assertEqual(self.character.model3d_autofit_version, 7)
        self.assertTrue(self.character.model3d_autofit_done)
        self.assertEqual(restored.before_snapshot["model3d_asset_id"], str(newer.pk))
        state = reconstruction_state(self.character, ensure=True)
        self.assertEqual(state["asset_id"], str(original.pk))
        self.assertEqual(state["status"], "ready")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["reconstruction"]["asset_id"], str(original.pk),
        )

    def test_existing_nested_reviewed_snapshot_restores_asset(self):
        original = self._asset()
        revision = self._revision(original)
        revision.after_snapshot.pop("model3d_asset_id")
        revision.save(update_fields=["after_snapshot"])
        self._revision(self._asset())
        self._restore(revision)
        self.character.refresh_from_db()
        self.assertEqual(self.character.active_model3d_asset_id, original.pk)

    def test_legacy_params_only_preserves_live_asset_and_autofit_flags(self):
        revision = self._revision(self._asset())
        revision.after_snapshot = {"model3d_params": {"nose": {"noseWidth": -0.2}}}
        revision.save(update_fields=["after_snapshot"])
        current = self._asset()
        self._revision(current)
        self._restore(revision)
        self.character.refresh_from_db()
        self.assertEqual(self.character.active_model3d_asset_id, current.pk)
        self.assertEqual(self.character.model3d_autofit_version, 7)
        self.assertEqual(
            self.character.model3d_params, revision.after_snapshot["model3d_params"],
        )

    def test_incompatible_pin_is_ignored_after_references_change(self):
        old = self._asset()
        revision = self._revision(old)
        reference = self.character.assets.get(asset_type="portrait")
        reference.metadata = {"sha256": "changed"}
        reference.save()
        latest = self._asset()
        state = reconstruction_state(self.character)
        self.assertEqual(state["asset_id"], str(latest.pk))
        with self.assertRaises(ValidationError):
            self._restore(revision)
        self.character.refresh_from_db()
        self.assertEqual(self.character.active_model3d_asset_id, old.pk)

    def test_missing_asset_restore_fails_without_any_mutation(self):
        revision = self._revision(self._asset())
        revision.after_snapshot["model3d_asset_id"] = str(uuid4())
        revision.save(update_fields=["after_snapshot"])
        self._revision(self._asset(), {"nose": {"noseWidth": 0.8}})
        before = model3d_snapshot(self.character)
        count = self.character.revisions.count()
        with self.assertRaises(ValidationError):
            self._restore(revision)
        self.character.refresh_from_db()
        self.assertEqual(model3d_snapshot(self.character), before)
        self.assertEqual(self.character.revisions.count(), count)

    def test_missing_or_changed_files_fail_explicit_restore(self):
        original = self._asset()
        revision = self._revision(original)
        hair_path = (
            Path(self.media.name) / original.metadata["hair_asset"]["storage_path"]
        )
        hair_path.write_bytes(b"modified-history")
        with self.assertRaisesMessage(ValidationError, "files have changed"):
            self._restore(revision)
        hair_path.unlink()
        with self.assertRaisesMessage(ValidationError, "files are unavailable"):
            self._restore(revision)

    def test_nonready_asset_restore_fails(self):
        original = self._asset()
        revision = self._revision(original)
        original.status = CharacterAssetStatus.FAILED
        original.save()
        with self.assertRaises(ValidationError):
            self._restore(revision)

    def test_cross_character_revision_and_asset_are_rejected(self):
        original = self._asset()
        revision = self._revision(original)
        other = self.create_character()
        with self.assertRaises(ValidationError):
            self.revisions.restore_revision(
                self.user_key, Action.EDIT_CONTENT, other, revision,
            )
        original.character = other
        original.save()
        with self.assertRaises(ValidationError):
            self._restore(revision)
        self.character.active_model3d_asset = original
        with self.assertRaises(ModelValidationError):
            self.character.save()

    def test_restore_requires_edit_permission(self):
        revision = self._revision(self._asset())
        outsider = UserKey.objects.create(
            user=User.objects.create_user(username="outsider3d"),
        )
        with self.assertRaises(PermissionDeniedError):
            self.revisions.restore_revision(
                outsider, Action.EDIT_CONTENT, self.character, revision,
            )

    def test_invalid_snapshot_params_and_flags_are_atomic(self):
        revision = self._revision(self._asset())
        initial = deepcopy(revision.after_snapshot)
        for key, invalid in (("model3d_params", []), ("model3d_autofit_done", 1),
                             ("model3d_autofit_version", -1)):
            revision.after_snapshot = {**initial, key: invalid}
            with self.assertRaises(ValidationError):
                self._restore(revision)
            self.character.refresh_from_db()
            self.assertEqual(self.character.model3d_params, initial["model3d_params"])

    def test_put_records_before_state_and_complete_after_state(self):
        asset = self._asset()
        self._revision(asset)
        before = model3d_snapshot(self.character)
        response = self.client.put(
            self._url(), {"params": {"nose": {"noseWidth": 0.5}}}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.character.refresh_from_db()
        revision = self.character.current_revision
        self.assertEqual(revision.before_snapshot, before)
        self.assertEqual(revision.after_snapshot["model3d_asset_id"], str(asset.pk))
        self.assertEqual(revision.after_snapshot["model3d_autofit_version"], 7)

    def test_put_failure_rolls_back_params(self):
        self._revision(self._asset())
        before = deepcopy(self.character.model3d_params)
        with patch.object(
            CharacterRevisionService, "create_revision",
            side_effect=ValidationError("test failure"),
        ):
            response = self.client.put(
                self._url(), {"params": {"nose": {"noseWidth": 1}}}, format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.character.refresh_from_db()
        self.assertEqual(self.character.model3d_params, before)

    def test_autofit_revision_records_flags_and_asset(self):
        asset = self._asset()
        before = model3d_snapshot(self.character)
        with patch("w_craft_back.character_studio.views.compute_autofit", return_value={
            "params": {"nose": {"noseWidth": 0.4}}, "sources": {}, "warnings": [],
        }):
            response = self.client.post(self._url() + "/autofit", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.character.refresh_from_db()
        revision = self.character.current_revision
        self.assertEqual(revision.before_snapshot, before)
        self.assertEqual(revision.after_snapshot["model3d_asset_id"], str(asset.pk))
        self.assertTrue(revision.after_snapshot["model3d_autofit_done"])

    def test_retry_creates_job_specific_paths_and_preserves_old_bytes(self):
        first_state = ensure_reconstruction(self.character)
        job = CharacterGenerationJob.objects.get(pk=first_state["job_id"])
        first = self.character.assets.get(source_job_id=job.pk)
        path = Path(self.media.name) / first.storage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"earlier-attempt")
        job.status = GenerationJobStatus.FAILED
        job.save()
        first.status = CharacterAssetStatus.FAILED
        first.save()
        next_state = retry_reconstruction(self.character)
        second = self.character.assets.get(source_job_id=next_state["job_id"])
        self.assertNotEqual(first.storage_path, second.storage_path)
        self.assertIn(str(job.pk), first.storage_path)
        self.assertIn(next_state["job_id"], second.storage_path)
        self.assertEqual(path.read_bytes(), b"earlier-attempt")

    def test_authenticated_api_save_restore_and_signed_head_hair_delivery(self):
        original = self._asset()
        first = self._revision(original)
        self._revision(self._asset(), {"eyes": {"eyeSize": -0.4}})
        response = self.client.put(
            self._url(), {"params": {"nose": {"noseWidth": 0.5}}}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        base_url = self._url().removesuffix("/model3d")
        response = self.client.get(base_url + "/revisions")
        self.assertEqual(response.status_code, 200, response.content)
        listed = response.json()
        self.assertTrue(any(row["revision_id"] == str(first.pk) for row in listed))
        response = self.client.post(
            base_url + f"/revisions/{first.pk}/restore", {}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            response.json()["after_snapshot"]["model3d_asset_id"], str(original.pk),
        )
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200, response.content)
        state = response.json()["reconstruction"]
        self.assertEqual(state["asset_id"], str(original.pk))
        for url, storage_key in (
            (state["model_url"], original.storage_path),
            (state["hair_url"], original.metadata["hair_asset"]["storage_path"]),
        ):
            signed_path = urlparse(url).path
            self.assertEqual(resolve(signed_path).url_name, "signed-media")
            media = self.client.get(signed_path)
            self.assertEqual(media.status_code, 200)
            self.assertEqual(
                b"".join(media.streaming_content),
                (Path(self.media.name) / storage_key).read_bytes(),
            )

    def _candidate(self, name):
        document = json.dumps({
            "asset": {"version": "2.0"},
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
            "buffers": [{"byteLength": 36}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36}],
            "accessors": [{
                "bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
            }],
        }).encode()
        document += b" " * (-len(document) % 4)
        binary = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
        chunks = struct.pack("<II", len(document), 0x4E4F534A) + document
        chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
        data = b"glTF" + struct.pack("<II", 2, 12 + len(chunks)) + chunks
        path = Path(self.media.name) / f"candidate-{name}.glb"
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    def _publish_options(self):
        asset = self._asset()
        revision = self._revision(asset)
        head, head_digest = self._candidate("head")
        hair, hair_digest = self._candidate("hair")
        return {
            "actor": self.user_key, "character_id": self.character.pk,
            "expected_revision": revision.revision_number,
            "expected_asset_id": str(asset.pk),
            "head_path": head, "head_sha256": head_digest,
            "hair_path": hair, "hair_sha256": hair_digest,
            "params": {"outfit_top": {"style": "polo"}, "eyes": {"eyeSize": 0.2}},
        }

    def test_publish_dry_run_is_read_only_then_apply_is_restorable(self):
        options = self._publish_options()
        previous = self.character.current_revision
        original_path = (
            Path(self.media.name) / self.character.active_model3d_asset.storage_path
        )
        original_bytes = original_path.read_bytes()
        count = self.character.assets.count()
        report = publish_reviewed_model3d(**options)
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(self.character.assets.count(), count)
        self.character.refresh_from_db()
        self.assertEqual(self.character.current_revision_id, previous.pk)
        report = publish_reviewed_model3d(**options, apply=True)
        self.character.refresh_from_db()
        self.assertEqual(
            str(self.character.active_model3d_asset_id), report["asset_id"],
        )
        self.assertEqual(self.character.model3d_params, options["params"])
        self.assertEqual(
            self.character.current_revision.revision_number,
            previous.revision_number + 1,
        )
        self.assertIn(
            "appearance-" + report["asset_id"], report["storage_paths"]["head"],
        )
        self._restore(previous)
        self.character.refresh_from_db()
        self.assertEqual(
            str(self.character.active_model3d_asset_id), options["expected_asset_id"],
        )
        self.assertEqual(original_path.read_bytes(), original_bytes)

    def test_publish_stale_review_or_changed_bytes_rejected_without_mutation(self):
        options = self._publish_options()
        revision_id = self.character.current_revision_id
        count = self.character.assets.count()
        for overrides in (
            {"expected_revision": 9999}, {"expected_asset_id": str(uuid4())},
            {"head_sha256": "bad"},
        ):
            with self.assertRaises(ValidationError):
                publish_reviewed_model3d(**{**options, **overrides}, apply=True)
        self.character.refresh_from_db()
        self.assertEqual(self.character.current_revision_id, revision_id)
        self.assertEqual(self.character.assets.count(), count)

    def test_publish_command_defaults_to_read_only(self):
        options = self._publish_options()
        params_path = Path(self.media.name) / "params.json"
        params_path.write_text(json.dumps(options["params"]), encoding="utf-8")
        out = StringIO()
        call_command(
            "publish_reviewed_model3d", character_id=str(self.character.pk),
            actor_id=self.user_key.pk, expected_revision=options["expected_revision"],
            expected_asset_id=options["expected_asset_id"],
            head=options["head_path"], hair=options["hair_path"],
            head_sha256=options["head_sha256"], hair_sha256=options["hair_sha256"],
            params=params_path, stdout=out,
        )
        self.assertEqual(json.loads(out.getvalue())["mode"], "dry_run")

    def test_stale_worker_cannot_overwrite_newer_published_files(self):
        state = ensure_reconstruction(self.character)
        job = CharacterGenerationJob.objects.get(pk=state["job_id"])
        asset = self.character.assets.get(source_job_id=job.pk)
        target = Path(self.media.name) / asset.storage_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"newer-lease-head")
        target.with_name("hair.glb").write_bytes(b"newer-lease-hair")

        def pipeline(job, references, output_path, work_dir, runner):
            output_path.write_bytes(b"stale-head")
            output_path.with_name("hair.glb").write_bytes(b"stale-hair")
            CharacterGenerationJob.objects.filter(pk=job.pk).update(
                lease_token=uuid4(), status=GenerationJobStatus.COMPLETED,
            )
            return {}

        with patch(
            "w_craft_back.character_studio.services."
            "model3d_reconstruction_service._execute_pipeline", side_effect=pipeline,
        ):
            self.assertIsNone(run_reconstruction_job(job.pk))
        self.assertEqual(target.read_bytes(), b"newer-lease-head")
        self.assertEqual(target.with_name("hair.glb").read_bytes(), b"newer-lease-hair")
