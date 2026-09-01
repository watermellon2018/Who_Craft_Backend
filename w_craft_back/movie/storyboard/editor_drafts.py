"""Validated, revision-checked working copies for the browser storyboard editor."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from django.db import transaction
from rest_framework import serializers

from w_craft_back.movie.project import policy
from w_craft_back.movie.project.dashboard_models import Scene
from w_craft_back.movie.storyboard.errors import StoryboardError, validation_error
from w_craft_back.movie.storyboard.models import SceneStoryboardEditorDraft
from w_craft_back.movie.storyboard.services import (
    SceneStoryboardContextService,
    _require_project,
    _scene,
)


MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MOVEMENTS = (
    "Static", "Dolly In", "Dolly Out", "Pan", "Pan Left", "Pan Right",
    "Tilt Up", "Tilt Down", "Orbit Left", "Orbit Right", "Truck Left",
    "Truck Right", "Crane Up", "Crane Down", "Follow", "Custom",
)


def _reject_embedded_media(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _reject_embedded_media(item)
    elif isinstance(value, list):
        for item in value:
            _reject_embedded_media(item)
    elif isinstance(value, str):
        stripped = value.lstrip().lower()
        if stripped.startswith(("data:", "blob:", "mock:")):
            raise serializers.ValidationError(
                "Embedded media cannot be saved in a draft."
            )
        if stripped.startswith(("https://", "http://")):
            try:
                parsed = urlsplit(value.strip())
                keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
                credentials = bool(parsed.username or parsed.password) or any(
                    key.startswith(("x-amz-", "x-goog-"))
                    or key in {"token", "access_token", "signature", "sig", "key"}
                    for key in keys
                )
            except ValueError as error:
                raise serializers.ValidationError("Invalid draft URL.") from error
            if credentials:
                raise serializers.ValidationError(
                    "Credential URLs cannot be saved in a draft."
                )


class StrictSerializer(serializers.Serializer):
    """Reject unknown fields, including accidentally copied credentials/media."""

    def to_internal_value(self, data: Any) -> dict:
        if isinstance(data, dict) and set(data) - set(self.fields):
            raise serializers.ValidationError({
                "nonFieldErrors": ["Unexpected fields are not allowed."],
            })
        return super().to_internal_value(data)


class IdentifierField(serializers.RegexField):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(regex=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$", **kwargs)


class FiniteFloatField(serializers.FloatField):
    def to_internal_value(self, data: Any) -> float:
        value = super().to_internal_value(data)
        if not math.isfinite(value):
            raise serializers.ValidationError("A finite number is required.")
        return value


class CompositionSerializer(StrictSerializer):
    """Editor rectangle percentages; structured render APIs use other units."""

    subjectId = IdentifierField()
    x = FiniteFloatField(min_value=0, max_value=100)
    y = FiniteFloatField(min_value=0, max_value=100)
    width = FiniteFloatField(min_value=0, max_value=100)
    height = FiniteFloatField(min_value=0, max_value=100)


class OtsSerializer(StrictSerializer):
    foregroundSubjectId = IdentifierField(required=False)
    shoulder = serializers.ChoiceField(choices=("left", "right"))
    targetId = IdentifierField(required=False)


class DraftCameraIntentSerializer(StrictSerializer):
    azimuth = serializers.ChoiceField(choices=(
        "front", "front-left", "left", "back-left", "back", "back-right",
        "right", "front-right",
    ))
    elevation = serializers.ChoiceField(choices=("low", "eye-level", "high", "top"))
    distance = serializers.ChoiceField(choices=("wide", "medium", "near"))
    framing = serializers.ChoiceField(choices=(
        "extreme-wide", "wide", "full", "medium", "medium-close", "close",
        "extreme-close", "ots", "pov",
    ))
    lens = FiniteFloatField(required=False, min_value=1, max_value=2000)
    targetId = IdentifierField(required=False)
    composition = CompositionSerializer(many=True, required=False, max_length=100)
    ots = OtsSerializer(required=False)


class DraftReferenceSerializer(StrictSerializer):
    id = IdentifierField()
    title = serializers.CharField(
        max_length=500, allow_blank=True, trim_whitespace=False,
    )
    type = serializers.ChoiceField(choices=(
        "character", "location", "object", "clothing", "other",
        "previous-keyframe", "previous-shot",
    ))
    primary = serializers.BooleanField(required=False)
    sourceKeyframeId = IdentifierField(required=False)
    sourceShotId = IdentifierField(required=False)
    versionId = IdentifierField(required=False)
    assetId = IdentifierField(required=False)


class CanvasPointSerializer(StrictSerializer):
    x = FiniteFloatField(min_value=0, max_value=100)
    y = FiniteFloatField(min_value=0, max_value=100)


class CanvasEntitySerializer(StrictSerializer):
    id = IdentifierField()
    type = serializers.ChoiceField(choices=(
        "character", "location", "object", "clothing", "other",
    ))
    title = serializers.CharField(max_length=2000, allow_blank=True)
    versionId = IdentifierField(required=False)
    assetId = IdentifierField(required=False)


class CanvasMotionSerializer(StrictSerializer):
    type = serializers.ChoiceField(choices=("static", "path"))
    points = CanvasPointSerializer(many=True, max_length=8)
    start = FiniteFloatField(min_value=0, max_value=86400)
    end = FiniteFloatField(min_value=0, max_value=86400)
    facing = serializers.CharField(max_length=2000, allow_blank=True)

    def validate(self, attrs: dict) -> dict:
        if attrs["end"] < attrs["start"]:
            raise serializers.ValidationError("Motion end precedes its start.")
        return attrs


class CanvasObjectSerializer(CanvasPointSerializer):
    id = IdentifierField()
    kind = serializers.ChoiceField(choices=(
        "person", "animal", "prop", "rectangle", "ellipse", "line",
    ))
    width = FiniteFloatField(min_value=0.1, max_value=100)
    height = FiniteFloatField(min_value=0.1, max_value=100)
    rotation = FiniteFloatField(min_value=-360, max_value=360)
    flipX = serializers.BooleanField()
    hidden = serializers.BooleanField()
    locked = serializers.BooleanField()
    title = serializers.CharField(
        max_length=2000, allow_blank=True, trim_whitespace=False,
    )
    description = serializers.CharField(
        max_length=2000, allow_blank=True, trim_whitespace=False,
    )
    comment = serializers.CharField(
        max_length=2000, allow_blank=True, trim_whitespace=False,
    )
    pose = serializers.ChoiceField(choices=("front", "profile", "back", "sitting"))
    entity = CanvasEntitySerializer(required=False)
    motion = CanvasMotionSerializer()


class CanvasMarkerSerializer(CanvasPointSerializer):
    id = IdentifierField()
    text = serializers.CharField(
        max_length=2000, allow_blank=True, trim_whitespace=False,
    )


class CanvasCameraMotionSerializer(StrictSerializer):
    type = serializers.ChoiceField(choices=(*MOVEMENTS, "Zoom In", "Zoom Out"))
    targetId = IdentifierField(required=False)
    intensity = serializers.ChoiceField(choices=("low", "medium", "high"))
    points = CanvasPointSerializer(
        many=True, required=False, default=list, max_length=8,
    )
    start = FiniteFloatField(min_value=0, max_value=86400)
    end = FiniteFloatField(min_value=0, max_value=86400)

    def validate(self, attrs: dict) -> dict:
        if attrs["end"] < attrs["start"]:
            raise serializers.ValidationError("Motion end precedes its start.")
        return attrs


class CanvasLightingSerializer(StrictSerializer):
    preset = serializers.ChoiceField(choices=("daylight", "studio", "night", "custom"))
    direction = serializers.ChoiceField(choices=(
        "front", "left", "right", "top-left", "top-right", "back", "top",
    ))
    softness = serializers.ChoiceField(choices=("soft", "hard"))
    temperature = serializers.ChoiceField(choices=("warm", "neutral", "cool"))
    contrast = serializers.ChoiceField(choices=("low", "medium", "high"))
    notes = serializers.CharField(
        max_length=2000, allow_blank=True, trim_whitespace=False,
    )


class CanvasDocumentSerializer(StrictSerializer):
    version = serializers.ChoiceField(choices=(1,))
    aspectRatio = serializers.ChoiceField(choices=("16:9", "9:16", "1:1"))
    objects = CanvasObjectSerializer(many=True, max_length=80)
    cameraMotion = CanvasCameraMotionSerializer()
    lighting = CanvasLightingSerializer()
    notes = serializers.CharField(
        max_length=2000, allow_blank=True, trim_whitespace=False,
    )
    markers = CanvasMarkerSerializer(many=True, max_length=30)

    def validate(self, attrs: dict) -> dict:
        ids = [item["id"] for item in (*attrs["objects"], *attrs["markers"])]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Canvas IDs must be unique.")
        target = attrs["cameraMotion"].get("targetId")
        if target and target not in {item["id"] for item in attrs["objects"]}:
            raise serializers.ValidationError("Unknown camera motion target.")
        return attrs


class DraftKeyframeSerializer(StrictSerializer):
    id = IdentifierField()
    shotId = IdentifierField()
    position = FiniteFloatField(min_value=0, max_value=1)
    type = serializers.ChoiceField(choices=("start", "intermediate", "end"))
    generationStatus = serializers.ChoiceField(choices=("idle", "ready", "failed"))
    cameraIntent = DraftCameraIntentSerializer()
    canvas = CanvasDocumentSerializer(required=False)
    generationReferences = DraftReferenceSerializer(
        many=True, required=False, max_length=100,
    )


class DraftTransitionSerializer(StrictSerializer):
    id = IdentifierField()
    fromKeyframeId = IdentifierField()
    toKeyframeId = IdentifierField()
    movementOverride = serializers.ChoiceField(choices=MOVEMENTS, required=False)


class SourceSegmentSerializer(StrictSerializer):
    id = IdentifierField()
    text = serializers.CharField(
        max_length=500000, allow_blank=True, trim_whitespace=False,
    )


class SourceDocumentSerializer(StrictSerializer):
    contentHash = serializers.RegexField(regex=r"^[a-f0-9]{64}$")
    sceneId = serializers.IntegerField(min_value=1)
    sceneVersion = serializers.IntegerField(min_value=1)
    segments = SourceSegmentSerializer(many=True, max_length=20000)
    truncated = serializers.BooleanField()

    def validate(self, attrs: dict) -> dict:
        segments = attrs["segments"]
        if len({segment["id"] for segment in segments}) != len(segments):
            raise serializers.ValidationError("Source segment IDs must be unique.")
        text = "".join(segment["text"] for segment in segments)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != attrs["contentHash"]:
            raise serializers.ValidationError("Source content hash does not match.")
        return attrs


class SourceRangeSerializer(StrictSerializer):
    start = serializers.IntegerField(min_value=0)
    end = serializers.IntegerField(min_value=1)


class DraftSourceSerializer(StrictSerializer):
    document = SourceDocumentSerializer()
    segmentIds = serializers.ListField(child=IdentifierField(), max_length=20000)
    ranges = SourceRangeSerializer(many=True, required=False, max_length=1000)
    origin = serializers.ChoiceField(choices=("manual", "ai"), required=False)

    def validate(self, attrs: dict) -> dict:
        segments = attrs["document"]["segments"]
        ids = attrs["segmentIds"]
        if len(ids) != len(set(ids)) or set(ids) - {item["id"] for item in segments}:
            raise serializers.ValidationError("Invalid source segment references.")
        length = sum(len(segment["text"]) for segment in segments)
        for interval in attrs.get("ranges", []):
            if not interval["start"] < interval["end"] <= length:
                raise serializers.ValidationError("Source range is out of bounds.")
        return attrs


class DraftShotSerializer(StrictSerializer):
    id = IdentifierField()
    sceneId = IdentifierField()
    title = serializers.CharField(
        max_length=255, allow_blank=True, trim_whitespace=False,
    )
    description = serializers.CharField(
        max_length=8000, allow_blank=True, trim_whitespace=False,
    )
    order = serializers.IntegerField(min_value=1, max_value=500)
    duration = FiniteFloatField(required=False, min_value=0, max_value=86400)
    characterIds = serializers.ListField(child=IdentifierField(), max_length=100)
    referenceIds = serializers.ListField(child=IdentifierField(), max_length=100)
    locationId = IdentifierField(required=False)
    keyframes = DraftKeyframeSerializer(many=True, max_length=100)
    transitions = DraftTransitionSerializer(many=True, max_length=100)
    source = DraftSourceSerializer(required=False)

    def validate(self, attrs: dict) -> dict:
        keyframes = attrs["keyframes"]
        keyframe_ids = {item["id"] for item in keyframes}
        if len(keyframe_ids) != len(keyframes):
            raise serializers.ValidationError("Keyframe IDs must be unique.")
        if any(item["shotId"] != attrs["id"] for item in keyframes):
            raise serializers.ValidationError("Keyframe belongs to another shot.")
        transitions = attrs["transitions"]
        if len({item["id"] for item in transitions}) != len(transitions):
            raise serializers.ValidationError("Transition IDs must be unique.")
        if any(
            item["fromKeyframeId"] not in keyframe_ids
            or item["toKeyframeId"] not in keyframe_ids
            or item["fromKeyframeId"] == item["toKeyframeId"]
            for item in transitions
        ):
            raise serializers.ValidationError("Invalid transition references.")
        for name in ("characterIds", "referenceIds"):
            if len(attrs[name]) != len(set(attrs[name])):
                raise serializers.ValidationError(f"{name} must be unique.")
        return attrs


class EditorDraftPayloadSerializer(StrictSerializer):
    schemaVersion = serializers.ChoiceField(choices=(1,))
    stage = serializers.ChoiceField(choices=("selection", "builder", "editor"))
    shots = DraftShotSerializer(many=True, max_length=500)

    def to_internal_value(self, data: Any) -> dict:
        try:
            encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                raise serializers.ValidationError("Draft exceeds the 2 MiB limit.")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise serializers.ValidationError("Invalid JSON draft.") from error
        _reject_embedded_media(data)
        return super().to_internal_value(data)

    def validate(self, attrs: dict) -> dict:
        shots = attrs["shots"]
        if len({shot["id"] for shot in shots}) != len(shots):
            raise serializers.ValidationError("Shot IDs must be unique.")
        keyframe_ids = [item["id"] for shot in shots for item in shot["keyframes"]]
        if len(set(keyframe_ids)) != len(keyframe_ids):
            raise serializers.ValidationError("Keyframe IDs must be unique in a scene.")
        if sorted(shot["order"] for shot in shots) != list(range(1, len(shots) + 1)):
            raise serializers.ValidationError("Shot order must be contiguous.")
        return attrs


class EditorDraftPutSerializer(StrictSerializer):
    expectedRevision = serializers.IntegerField(min_value=0)
    mutationId = serializers.UUIDField()
    payload = EditorDraftPayloadSerializer()


def _entry(draft: SceneStoryboardEditorDraft) -> dict[str, Any]:
    return {
        "sceneId": draft.scene_id,
        "revision": draft.revision,
        "payload": draft.payload,
    }


def list_editor_drafts(*, actor: Any, project_id: int) -> dict[str, Any]:
    project = _require_project(
        actor=actor, project_id=project_id, action=policy.Action.VIEW,
    )
    return {
        "userId": actor.pk,
        "canEdit": policy.can(actor, project, policy.Action.EDIT_CONTENT),
        "drafts": [
            _entry(draft) for draft in SceneStoryboardEditorDraft.objects.filter(
                scene__project=project,
            ).order_by("scene__order", "scene_id")
        ],
    }


def _validate_scene_sources(payload: dict, scene: Scene) -> None:
    canonical_text = None
    for shot in payload["shots"]:
        if shot["sceneId"] != str(scene.pk):
            raise validation_error({"payload": "Shot belongs to another scene."})
        source = shot.get("source")
        if source is None:
            continue
        document = source["document"]
        if document["sceneId"] != scene.pk or document["sceneVersion"] > scene.version:
            raise validation_error({
                "source": "Source belongs to another scene/version.",
            })
        if document["sceneVersion"] == scene.version:
            if canonical_text is None:
                canonical_text = SceneStoryboardContextService.scene_text(scene)
            source_text = "".join(item["text"] for item in document["segments"])
            if source_text != canonical_text:
                raise validation_error({
                    "source": "Source differs from the current scene.",
                })


@transaction.atomic
def save_editor_draft(
    *, actor: Any, project_id: int, scene_id: int,
    expected_revision: int, mutation_id: UUID, payload: dict,
) -> dict[str, Any]:
    """Serialize all writes for a scene, including concurrent first saves."""
    project = _require_project(
        actor=actor, project_id=project_id, action=policy.Action.EDIT_CONTENT,
    )
    scene = _scene(project, scene_id, lock=True)
    draft = SceneStoryboardEditorDraft.objects.select_for_update().filter(
        scene=scene,
    ).first()
    if draft is not None and draft.last_mutation_id == mutation_id:
        if draft.payload != payload:
            raise validation_error({"mutationId": "Mutation ID was already used."})
        return _entry(draft)
    current_revision = draft.revision if draft is not None else 0
    if expected_revision != current_revision:
        raise StoryboardError(
            "Another editor has saved a newer storyboard draft.",
            code="STORYBOARD_DRAFT_CONFLICT", http_status=409,
            errors={"currentRevision": current_revision},
        )
    _validate_scene_sources(payload, scene)
    if draft is None:
        draft = SceneStoryboardEditorDraft(scene=scene)
    draft.payload = payload
    draft.revision = current_revision + 1
    draft.last_mutation_id = mutation_id
    draft.updated_by = actor
    draft.save()
    return _entry(draft)
