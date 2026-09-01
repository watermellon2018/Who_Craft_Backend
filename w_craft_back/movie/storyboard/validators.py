"""Validation for structured camera targets and screen-space composition."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from django.core.exceptions import ValidationError

from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.reference_library.models import ProjectReference


TARGET_TYPES = {"character", "group", "visual_asset", "center"}
COMPOSITION_SUBJECT_TYPES = {"character", "visual_asset"}
MAX_CAMERA_TARGET_IDS = 100
MAX_COMPOSITION_ITEMS = 100
MAX_CAMERA_METADATA_BYTES = 16 * 1024


def _string_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError("ids must be a list")
    result = [str(item).strip() for item in value]
    if len(result) > MAX_CAMERA_TARGET_IDS:
        raise ValidationError(
            f"ids cannot contain more than {MAX_CAMERA_TARGET_IDS} values"
        )
    if any(not item for item in result):
        raise ValidationError("ids cannot contain empty values")
    if len(result) != len(set(result)):
        raise ValidationError("ids must be unique")
    return result


def _validate_project_subjects(
    *,
    project_id: int,
    subject_type: str,
    ids: Iterable[str],
) -> None:
    subject_ids = list(ids)
    if subject_type in {"character", "group", "center"}:
        found = set(
            str(value)
            for value in StudioCharacter.objects.filter(
                project_id=project_id,
                character_id__in=subject_ids,
            ).values_list("character_id", flat=True)
        )
    else:
        found = set(
            str(value)
            for value in ProjectReference.objects.filter(
                project_id=project_id,
                id__in=subject_ids,
                archived_at__isnull=True,
            ).values_list("id", flat=True)
        )
    missing = [value for value in subject_ids if value not in found]
    if missing:
        raise ValidationError(
            f"Subjects are missing or belong to another project: {', '.join(missing)}"
        )


def validate_camera_target(*, project_id: int, target: Any) -> dict[str, Any]:
    if not isinstance(target, Mapping):
        raise ValidationError("target must be an object")
    target_type = str(target.get("type") or "").strip().lower()
    if target_type not in TARGET_TYPES:
        raise ValidationError("target.type is invalid")
    ids = _string_ids(target.get("ids"))
    if target_type in {"character", "visual_asset"} and len(ids) != 1:
        raise ValidationError(f"{target_type} target requires exactly one id")
    if target_type in {"group", "center"} and len(ids) < 2:
        raise ValidationError(f"{target_type} target requires at least two ids")
    _validate_project_subjects(
        project_id=project_id,
        subject_type=target_type,
        ids=ids,
    )
    return {"type": target_type, "ids": ids}


def _number(item: Mapping[str, Any], field: str) -> float:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number")
    return float(value)


def validate_composition(
    *,
    project_id: int,
    composition: Any,
) -> list[dict[str, Any]]:
    if not isinstance(composition, list):
        raise ValidationError("composition must be a list")
    if len(composition) > MAX_COMPOSITION_ITEMS:
        raise ValidationError(
            "composition cannot contain more than "
            f"{MAX_COMPOSITION_ITEMS} items"
        )
    normalized: list[dict[str, Any]] = []
    subject_ids: dict[str, list[str]] = {
        subject_type: [] for subject_type in COMPOSITION_SUBJECT_TYPES
    }
    seen_subjects: set[tuple[str, str]] = set()
    for index, raw in enumerate(composition):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"composition[{index}] must be an object")
        subject_type = str(raw.get("subject_type") or "").strip().lower()
        if subject_type not in COMPOSITION_SUBJECT_TYPES:
            raise ValidationError(
                f"composition[{index}].subject_type is invalid"
            )
        subject_id = str(raw.get("subject_id") or "").strip()
        if not subject_id:
            raise ValidationError(
                f"composition[{index}].subject_id is required"
            )
        subject_key = (subject_type, subject_id)
        if subject_key in seen_subjects:
            raise ValidationError(
                f"composition[{index}] duplicates a subject"
            )
        seen_subjects.add(subject_key)
        subject_ids[subject_type].append(subject_id)
        x = _number(raw, "x")
        y = _number(raw, "y")
        width = _number(raw, "width")
        height = _number(raw, "height")
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValidationError(
                f"composition[{index}] x and y must be between 0 and 1"
            )
        if not 0 < width <= 1 or not 0 < height <= 1:
            raise ValidationError(
                f"composition[{index}] width and height must be in (0, 1]"
            )
        if x + width > 1 or y + height > 1:
            raise ValidationError(
                f"composition[{index}] must fit inside the normalized frame"
            )
        normalized.append(
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
        )
    for subject_type, ids in subject_ids.items():
        if ids:
            _validate_project_subjects(
                project_id=project_id,
                subject_type=subject_type,
                ids=ids,
            )
    return normalized


def validate_camera_metadata(
    *,
    project_id: int,
    framing: str,
    metadata: Any,
) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise ValidationError("cameraMetadata must be an object")
    result = dict(metadata)
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_CAMERA_METADATA_BYTES:
        raise ValidationError(
            "cameraMetadata cannot exceed "
            f"{MAX_CAMERA_METADATA_BYTES} encoded bytes"
        )
    if framing != "ots":
        return result
    required = ("foreground_subject_id", "target_subject_id", "shoulder")
    missing = [field for field in required if not result.get(field)]
    if missing:
        raise ValidationError(
            f"OTS metadata requires: {', '.join(missing)}"
        )
    if result["shoulder"] not in {"left", "right"}:
        raise ValidationError("OTS shoulder must be left or right")
    _validate_project_subjects(
        project_id=project_id,
        subject_type="character",
        ids=(
            str(result["foreground_subject_id"]),
            str(result["target_subject_id"]),
        ),
    )
    return result
