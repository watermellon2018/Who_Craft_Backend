"""Deterministic prompt normalization and compilation for film references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from w_craft_back.movie.reference_library.errors import ReferenceError


BRIEF_SCHEMA_VERSION = "reference_brief.v1"
ALLOWED_ASPECT_RATIOS = ("1:1", "4:3", "3:2", "16:9", "2:3")
LIST_FIELDS = (
    "materials",
    "palette",
    "distinctiveFeatures",
    "continuityProperties",
    "markings",
)
TEXT_FIELDS = (
    "description",
    "condition",
    "era",
    "style",
    "view",
    "dimensions",
    "continuityNotes",
    "negativePrompt",
)
ALLOWED_FIELDS = {
    "schemaVersion",
    "aspectRatio",
    *LIST_FIELDS,
    *TEXT_FIELDS,
}


@dataclass(frozen=True)
class CompiledReferencePrompt:
    """Provider-neutral immutable prompt snapshot."""

    compiled_prompt: str
    negative_prompt: str
    metadata: dict[str, Any]
    schema_version: str


def _clean_text(value: Any, *, max_length: int, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReferenceError(
            f"{field} must be a string.",
            code="REFERENCE_INVALID_BRIEF",
            errors={field: ["must be a string"]},
        )
    normalized = " ".join(value.split())
    if len(normalized) > max_length:
        raise ReferenceError(
            f"{field} is too long.",
            code="REFERENCE_INVALID_BRIEF",
            errors={field: [f"must contain at most {max_length} characters"]},
        )
    return normalized


def _clean_list(value: Any, *, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ReferenceError(
            f"{field} must be a list with at most 20 items.",
            code="REFERENCE_INVALID_BRIEF",
            errors={field: ["must be a list with at most 20 items"]},
        )
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _clean_text(item, max_length=100, field=field)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def normalize_brief(value: Any) -> dict[str, Any]:
    """Validate and normalize a ``reference_brief.v1`` JSON object."""

    if value in (None, ""):
        value = {}
    if not isinstance(value, dict):
        raise ReferenceError(
            "brief must be an object.",
            code="REFERENCE_INVALID_BRIEF",
            errors={"brief": ["must be an object"]},
        )
    unknown = sorted(set(value) - ALLOWED_FIELDS)
    if unknown:
        raise ReferenceError(
            "brief contains unsupported fields.",
            code="REFERENCE_INVALID_BRIEF",
            errors={"brief": [f"unsupported fields: {', '.join(unknown)}"]},
        )
    schema_version = value.get("schemaVersion", BRIEF_SCHEMA_VERSION)
    if schema_version != BRIEF_SCHEMA_VERSION:
        raise ReferenceError(
            "Unsupported reference brief schema.",
            code="REFERENCE_INVALID_BRIEF",
            errors={"brief.schemaVersion": [f"must equal {BRIEF_SCHEMA_VERSION}"]},
        )
    aspect_ratio = value.get("aspectRatio", "1:1")
    if aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        raise ReferenceError(
            "Unsupported aspect ratio.",
            code="REFERENCE_INVALID_BRIEF",
            errors={"brief.aspectRatio": ["unsupported value"]},
        )

    normalized: dict[str, Any] = {
        "schemaVersion": BRIEF_SCHEMA_VERSION,
        "aspectRatio": aspect_ratio,
    }
    for field in TEXT_FIELDS:
        limit = 2000 if field in {"continuityNotes", "negativePrompt"} else 4000
        cleaned = _clean_text(value.get(field), max_length=limit, field=field)
        if cleaned:
            normalized[field] = cleaned
    for field in LIST_FIELDS:
        cleaned_list = _clean_list(value.get(field), field=field)
        if cleaned_list:
            normalized[field] = cleaned_list
    return normalized


def compile_reference_prompt(
    *,
    category: str,
    description: str,
    brief: dict[str, Any],
    edit_instruction: str = "",
) -> CompiledReferencePrompt:
    """Compile a stable provider-neutral continuity prompt."""

    normalized = normalize_brief(brief)
    subject_description = normalized.get("description") or _clean_text(
        description,
        max_length=4000,
        field="description",
    )
    if not subject_description:
        raise ReferenceError(
            "A description is required for generation.",
            code="REFERENCE_INVALID_BRIEF",
            errors={"description": ["this field is required"]},
        )

    anchors: list[str] = []
    for field in ("distinctiveFeatures", "dimensions", "materials", "markings"):
        value = normalized.get(field)
        if isinstance(value, list):
            value = ", ".join(value)
        if value:
            anchors.append(f"{field}: {value}")
    context: list[str] = []
    for field in ("era", "palette", "condition", "style"):
        value = normalized.get(field)
        if isinstance(value, list):
            value = ", ".join(value)
        if value:
            context.append(f"{field}: {value}")

    sections = [
        "[task]\nCreate a continuity reference image for a film production.",
        f"[subject]\ncategory: {category}\n{subject_description}",
        "[identity anchors]\n"
        + ("\n".join(anchors) or "Preserve a clear, repeatable identity."),
        "[context]\n" + ("\n".join(context) or "Neutral production-reference context."),
        (
            "[composition]\n"
            f"aspect ratio: {normalized['aspectRatio']}\n"
            f"view: {normalized.get('view', 'clear neutral presentation')}"
        ),
        "[continuity]\n" + (
            normalized.get("continuityNotes")
            or ", ".join(normalized.get("continuityProperties", []))
            or "Keep all identity-defining properties stable between scenes."
        ),
    ]
    instruction = _clean_text(
        edit_instruction,
        max_length=2000,
        field="editInstruction",
    )
    if instruction:
        sections.append(
            "[edit]\nPreserve every identity anchor and change only this: "
            + instruction
        )
    negative = normalized.get("negativePrompt", "")
    sections.append(
        "[avoid]\n"
        + (negative or "logos, text overlays, watermarks, ambiguous shapes")
    )
    return CompiledReferencePrompt(
        compiled_prompt="\n\n".join(sections),
        negative_prompt=negative,
        metadata={
            "aspectRatio": normalized["aspectRatio"],
            "operation": "edit" if instruction else "generate",
        },
        schema_version=BRIEF_SCHEMA_VERSION,
    )
