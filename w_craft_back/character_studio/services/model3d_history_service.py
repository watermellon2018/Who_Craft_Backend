"""Durable 3D snapshots and validation shared by saves and revision restore."""

from copy import deepcopy
import hashlib
from pathlib import PurePosixPath
from uuid import UUID

from django.core.files.storage import default_storage

from w_craft_back.character_studio.models import CharacterAsset, StudioCharacter
from w_craft_back.character_studio.services.errors import ValidationError
from w_craft_back.character_studio.services.model3d_service import (
    validate_model3d_params,
)


def model3d_snapshot(character: StudioCharacter) -> dict:
    """Capture the complete current 3D state using IDs/keys, never signed URLs."""
    from .model3d_reconstruction_service import (
        _reference_fingerprint,
        _selected_references,
        compatible_model3d_asset,
        selected_model3d_asset,
    )

    references = _selected_references(character)
    asset = selected_model3d_asset(character, references)
    fingerprint = _reference_fingerprint(references) if references else ""
    if not compatible_model3d_asset(character, asset, fingerprint):
        asset = None
    reconstruction = None
    if asset:
        hair = asset.metadata["hair_asset"]
        reconstruction = {
            "asset_id": str(asset.pk),
            "reference_fingerprint": fingerprint,
            "head_storage_path": asset.storage_path,
            "hair_storage_path": hair["storage_path"],
            "head_sha256": asset.metadata.get("sha256", ""),
            "hair_sha256": hair.get("sha256", ""),
        }
    return {
        "model3d_params": deepcopy(character.model3d_params or {}),
        "model3d_autofit_done": character.model3d_autofit_done,
        "model3d_autofit_version": character.model3d_autofit_version,
        "model3d_asset_id": str(asset.pk) if asset else None,
        "model3d_reconstruction": reconstruction,
    }


def _verify_stored_component(key: str, expected_digest: str = "") -> None:
    """Reject unavailable, changed, or unsafe historical media."""
    path = PurePosixPath(str(key).replace("\\", "/"))
    if not key or path.is_absolute() or ".." in path.parts or ":" in str(path):
        raise ValidationError("The revision's 3D storage path is invalid.")
    try:
        if not default_storage.exists(key) or default_storage.size(key) <= 0:
            raise ValidationError("The revision's 3D files are unavailable.")
        if expected_digest:
            digest = hashlib.sha256()
            with default_storage.open(key, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_digest:
                raise ValidationError("The revision's 3D files have changed.")
    except OSError as exc:
        raise ValidationError("The revision's 3D files are unavailable.") from exc


def validated_model3d_asset(
    character: StudioCharacter, asset_id: str, reconstruction: dict | None = None,
) -> CharacterAsset:
    """Validate an explicit selection; never silently substitute a newer asset."""
    from .model3d_reconstruction_service import (
        _reference_fingerprint,
        _selected_references,
        compatible_model3d_asset,
    )

    try:
        asset_uuid = UUID(str(asset_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("The revision's 3D asset identifier is invalid.") from exc
    asset = CharacterAsset.objects.filter(pk=asset_uuid).first()
    references = _selected_references(character)
    fingerprint = _reference_fingerprint(references) if references else ""
    if not compatible_model3d_asset(character, asset, fingerprint):
        raise ValidationError(
            "The revision's 3D asset is unavailable or incompatible "
            "with current references."
        )
    details = reconstruction or {}
    hair = asset.metadata["hair_asset"]
    expected_values = {
        "asset_id": str(asset.pk),
        "reference_fingerprint": fingerprint,
        "head_storage_path": asset.storage_path,
        "hair_storage_path": hair["storage_path"],
    }
    if any(
        details.get(key, value) != value for key, value in expected_values.items()
    ):
        raise ValidationError("The revision's 3D asset metadata has changed.")
    _verify_stored_component(
        asset.storage_path,
        details.get("head_sha256") or asset.metadata.get("sha256", ""),
    )
    _verify_stored_component(
        hair["storage_path"], details.get("hair_sha256") or hair.get("sha256", ""),
    )
    return asset


def restore_model3d_snapshot(character: StudioCharacter, snapshot: dict) -> None:
    """Apply validated snapshot fields in memory; caller owns locking/transaction."""
    if not isinstance(snapshot, dict):
        raise ValidationError("Revision snapshot must be an object.")
    if "model3d_params" in snapshot:
        character.model3d_params = validate_model3d_params(snapshot["model3d_params"])
    if "model3d_autofit_done" in snapshot:
        done = snapshot["model3d_autofit_done"]
        if not isinstance(done, bool):
            raise ValidationError("Revision autofit flag must be boolean.")
        character.model3d_autofit_done = done
    if "model3d_autofit_version" in snapshot:
        version = snapshot["model3d_autofit_version"]
        if (
            isinstance(version, bool) or not isinstance(version, int)
            or not 0 <= version <= 32767
        ):
            raise ValidationError("Revision autofit version is invalid.")
        character.model3d_autofit_version = version

    details = snapshot.get("model3d_reconstruction")
    if details is not None and not isinstance(details, dict):
        raise ValidationError("Revision reconstruction snapshot must be an object.")
    # Earlier reviewed local revisions used the nested key (including angry_dog
    # revision 109). A truly legacy snapshot has neither form and retains selection.
    has_selection = "model3d_asset_id" in snapshot or (
        isinstance(details, dict) and "asset_id" in details
    )
    if not has_selection:
        return
    asset_id = snapshot.get("model3d_asset_id", (details or {}).get("asset_id"))
    if asset_id is None:
        character.active_model3d_asset = None
    else:
        character.active_model3d_asset = validated_model3d_asset(
            character, asset_id, details,
        )
