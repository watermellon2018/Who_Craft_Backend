"""Exact screenplay snapshots used to attribute AI-proposed shots."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from w_craft_back.movie.project.dashboard_models import Scene


SOURCE_TEXT_BUDGET = 20000
_SEGMENT_BOUNDARY = re.compile(r'[.!?…]["\'»”’\)\]]*[^\S\r\n]+|\r\n|[\r\n]')


class SourceSegment(TypedDict):
    id: str
    text: str


class ShotListSource(TypedDict):
    scene_id: int
    scene_version: int
    content_hash: str
    segments: list[SourceSegment]
    truncated: bool


def _split_text(text: str) -> list[str]:
    """Split sentences/lines without removing any characters or whitespace."""
    parts: list[str] = []
    start = 0
    for boundary in _SEGMENT_BOUNDARY.finditer(text):
        parts.append(text[start:boundary.end()])
        start = boundary.end()
    if start < len(text):
        parts.append(text[start:])
    return parts


def build_source_snapshot(
    *, scene_id: int, scene_version: int, text: str,
) -> ShotListSource:
    """Keep the full canonical source, with a segment boundary at the AI limit."""
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    parts = _split_text(text[:SOURCE_TEXT_BUDGET])
    parts.extend(_split_text(text[SOURCE_TEXT_BUDGET:]))
    return {
        "scene_id": scene_id,
        "scene_version": scene_version,
        "content_hash": content_hash,
        "segments": [
            {"id": f"{content_hash[:12]}-{index + 1}", "text": part}
            for index, part in enumerate(parts)
        ],
        "truncated": len(text) > SOURCE_TEXT_BUDGET,
    }


def source_from_scene(scene: Scene) -> ShotListSource:
    """Snapshot an already-authorized scene using the existing text precedence."""
    from w_craft_back.movie.storyboard.services import SceneStoryboardContextService

    return build_source_snapshot(
        scene_id=scene.pk,
        scene_version=scene.version,
        text=SceneStoryboardContextService.scene_text(scene),
    )


def prompt_source_segments(source: ShotListSource) -> list[SourceSegment]:
    """Return only the exact prefix segments that may be passed to the model."""
    segments: list[SourceSegment] = []
    used = 0
    for segment in source["segments"]:
        used += len(segment["text"])
        if used > SOURCE_TEXT_BUDGET:
            break
        segments.append(segment)
    return segments
