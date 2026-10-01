"""Publish reviewed local GLBs as an immutable, restorable character revision."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
from uuid import uuid4

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Max

from w_craft_back.character_studio.models import (
    CharacterAsset, CharacterAssetStatus, CharacterAssetType, RevisionChangeType,
    StudioCharacter,
)
from .errors import ValidationError
from .model3d_autofit_service import MODEL3D_AUTOFIT_VERSION
from .model3d_history_service import model3d_snapshot, validated_model3d_asset
from .model3d_reconstruction_service import selected_model3d_asset
from .model3d_service import validate_model3d_params
from .permissions import get_project_for_action
from .revision_service import CharacterRevisionService
from w_craft_back.movie.project.policy import Action


def _reviewed_glb(path: Path, expected_sha256: str) -> tuple[bytes, dict]:
    """Accept only bounded, self-contained GLB v2 bytes matching review hashes."""
    if not path.is_file() or not 20 <= path.stat().st_size <= 100 * 1024 * 1024:
        raise ValidationError("Reviewed GLB must exist and be at most 100 MiB.")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise ValidationError("Reviewed GLB checksum does not match.")
    if data[:4] != b"glTF" or struct.unpack_from("<II", data, 4) != (2, len(data)):
        raise ValidationError("Reviewed file is not a valid GLB v2 container.")
    length, kind = struct.unpack_from("<II", data, 12)
    if kind != 0x4E4F534A or 20 + length > len(data):
        raise ValidationError("Reviewed GLB has an invalid JSON chunk.")
    try:
        document = json.loads(data[20:20 + length])
        external = [
            item.get("uri")
            for collection in ("buffers", "images")
            for item in document.get(collection, [])
            if item.get("uri") and not str(item["uri"]).startswith("data:")
        ]
        if external or not document.get("meshes"):
            raise ValueError("GLB needs embedded meshes and resources")
    except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
        raise ValidationError(
            "Reviewed GLB must contain self-contained mesh data."
        ) from exc
    return data, {"sha256": digest, "bytes": len(data)}


@transaction.atomic
def publish_reviewed_model3d(
    *, actor, character_id, expected_revision: int, expected_asset_id: str,
    params: dict, head_path: Path, head_sha256: str, hair_path: Path,
    hair_sha256: str, apply: bool = False, provenance: dict | None = None,
) -> dict:
    """Review or publish exact candidate bytes; permissions and stale guards apply.

    Dry runs perform no writes. Apply writes a new asset directory and revision,
    keeping every earlier asset intact. Failed DB commits can leave an unlinked
    UUID directory; it is deliberately not reused or silently deleted.
    """
    character = StudioCharacter.objects.select_for_update().get(pk=character_id)
    get_project_for_action(actor, character.project_id, Action.EDIT_CONTENT)
    if (
        not character.current_revision_id
        or character.current_revision.revision_number != expected_revision
    ):
        raise ValidationError("Character revision changed; review the candidate again.")
    original = selected_model3d_asset(character)
    if original is None or str(original.pk) != str(expected_asset_id):
        raise ValidationError(
            "Selected 3D asset changed; review the candidate again."
        )
    validated_model3d_asset(character, str(original.pk))
    cleaned = validate_model3d_params(params)
    if provenance is not None and not isinstance(provenance, dict):
        raise ValidationError("Candidate provenance must be an object.")
    head_bytes, head = _reviewed_glb(Path(head_path), head_sha256)
    hair_bytes, hair = _reviewed_glb(Path(hair_path), hair_sha256)
    before = model3d_snapshot(character)
    asset_id = uuid4()
    fingerprint = original.metadata["reference_fingerprint"]
    prefix = (
        f"character-studio/model3d/{character.pk}/{fingerprint}/"
        f"appearance-{asset_id}"
    )
    paths = {name: f"{prefix}/{name}.glb" for name in ("head", "hair")}
    report = {
        "mode": "apply" if apply else "dry_run", "character_id": str(character.pk),
        "expected_revision": expected_revision, "original_asset_id": str(original.pk),
        "reference_fingerprint": fingerprint, "head": head, "hair": hair,
        "params": cleaned, "autofit_version": MODEL3D_AUTOFIT_VERSION,
    }
    if not apply:
        return report
    for name, data in (("head", head_bytes), ("hair", hair_bytes)):
        if default_storage.exists(paths[name]):
            raise ValidationError(
                "Candidate output already exists; refusing to overwrite."
            )
        stored = default_storage.save(paths[name], ContentFile(data))
        if stored != paths[name]:
            raise ValidationError("Storage changed the immutable candidate path.")
    media_url = str(settings.MEDIA_URL).rstrip("/") + "/"
    version = (
        character.assets.filter(asset_type=CharacterAssetType.MODEL_3D)
        .aggregate(latest=Max("version"))["latest"] or 0
    ) + 1
    asset = CharacterAsset.objects.create(
        asset_id=asset_id, character=character, project=character.project, user=actor,
        asset_type=CharacterAssetType.MODEL_3D, status=CharacterAssetStatus.READY,
        image_url=media_url + paths["head"], storage_path=paths["head"],
        mime_type="model/gltf-binary", source="reviewed_reference",
        source_job_id=original.source_job_id,
        model_name=original.model_name,
        model_version=original.model_version, version=version,
        metadata={
            **deepcopy(original.metadata), **head,
            "derived_from_asset_id": str(original.pk),
            "reviewed_provenance": deepcopy(provenance or {}),
            "hair_asset": {
                **deepcopy(original.metadata["hair_asset"]), **hair,
                "asset_id": f"{asset_id}:hair", "storage_path": paths["hair"],
                "model_url": media_url + paths["hair"],
            },
        },
    )
    character.active_model3d_asset = asset
    character.model3d_params = cleaned
    character.model3d_autofit_done = True
    character.model3d_autofit_version = MODEL3D_AUTOFIT_VERSION
    character.save(update_fields=[
        "active_model3d_asset", "model3d_params", "model3d_autofit_done",
        "model3d_autofit_version", "updated_at",
    ])
    revision = CharacterRevisionService().create_revision(
        actor, Action.EDIT_CONTENT, character, RevisionChangeType.MANUAL_UPDATE,
        change_summary="model3d_reviewed_appearance", changed_region="full_character",
        before_snapshot=before, snapshot={"stage": "3d_model"},
    )
    return {
        **report, "asset_id": str(asset.pk), "asset_version": version,
        "revision_id": str(revision.pk), "revision_number": revision.revision_number,
        "storage_paths": paths,
    }
