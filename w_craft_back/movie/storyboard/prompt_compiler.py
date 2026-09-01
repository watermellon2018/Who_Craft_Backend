"""Provider-neutral Storyboard generation request compilation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


AZIMUTH_TEXT = {
    "front": "directly in front of",
    "front_left": "approximately 45 degrees front-left of",
    "left": "to the left of",
    "back_left": "approximately 45 degrees back-left of",
    "back": "behind",
    "back_right": "approximately 45 degrees back-right of",
    "right": "to the right of",
    "front_right": "approximately 45 degrees front-right of",
}
ELEVATION_TEXT = {
    "low": "from a low angle",
    "eye_level": "at eye level",
    "high": "from a high angle",
    "top": "from a top-down angle",
}
FRAMING_TEXT = {
    "extreme_wide": "Extreme wide shot",
    "wide": "Wide shot",
    "full": "Full shot",
    "medium": "Medium shot",
    "medium_close": "Medium close-up",
    "close": "Close-up",
    "extreme_close": "Extreme close-up",
    "ots": "Over-the-shoulder shot",
    "pov": "Point-of-view shot",
}
DISTANCE_TEXT = {
    "wide": "wide camera distance",
    "medium": "medium camera distance",
    "near": "near camera distance",
}


@dataclass(frozen=True)
class StoryboardGenerationRequest:
    scene_text: str
    shot_description: str
    location: dict[str, Any] | None
    characters: list[dict[str, Any]]
    visual_assets: list[dict[str, Any]]
    camera_intent: dict[str, Any]
    composition: list[dict[str, Any]]
    primary_reference: dict[str, Any] | None
    additional_references: list[dict[str, Any]]
    style_reference: dict[str, Any] | None

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


def _target_label(target: Mapping[str, Any]) -> str:
    label = str(target.get("label") or "").strip()
    if label:
        return label
    ids = target.get("ids")
    if isinstance(ids, list) and ids:
        return ", ".join(str(item) for item in ids)
    return "the selected subject"


class CameraIntentPromptBuilder:
    """Translate structured camera intent only at the provider boundary."""

    @classmethod
    def build(cls, intent: Mapping[str, Any]) -> str:
        target = intent.get("target")
        target_mapping = target if isinstance(target, Mapping) else {}
        subject = _target_label(target_mapping)
        framing = FRAMING_TEXT.get(str(intent.get("framing")), "Cinematic shot")
        azimuth = AZIMUTH_TEXT.get(str(intent.get("azimuth")), "relative to")
        elevation = ELEVATION_TEXT.get(str(intent.get("elevation")), "")
        distance = DISTANCE_TEXT.get(str(intent.get("distance")), "")
        lens = intent.get("lens_mm")
        pieces = [
            f"{framing} of {subject}.",
            f"Camera positioned {azimuth} {subject}, {elevation}.",
            f"Use a {distance}.",
        ]
        if lens:
            pieces.append(f"Use a natural {int(lens)} mm lens perspective.")
        return " ".join(piece for piece in pieces if piece)


class CompositionPromptBuilder:
    """Describe normalized placement without replacing the structured input."""

    @staticmethod
    def build(composition: list[Mapping[str, Any]]) -> str:
        instructions: list[str] = []
        for item in composition:
            subject = str(
                item.get("subject_label")
                or item.get("subject_id")
                or "Subject"
            )
            x = float(item.get("x", 0))
            width = float(item.get("width", 0))
            height = float(item.get("height", 0))
            center = x + width / 2
            if center < 0.4:
                horizontal = "on the left third"
            elif center > 0.6:
                horizontal = "on the right third"
            else:
                horizontal = "near the horizontal center"
            instructions.append(
                f"Place {subject} {horizontal}; it should occupy approximately "
                f"{round(height * 100)}% of the image height."
            )
        return " ".join(instructions)


def compile_storyboard_prompt(request: StoryboardGenerationRequest) -> str:
    camera = CameraIntentPromptBuilder.build(request.camera_intent)
    composition = CompositionPromptBuilder.build(request.composition)
    character_names = ", ".join(
        str(item.get("name"))
        for item in request.characters
        if item.get("name")
    )
    assets = ", ".join(
        str(item.get("title"))
        for item in request.visual_assets
        if item.get("title")
    )
    sections = [
        "Create one cinematic storyboard still image. Do not create video.",
        f"Scene context: {request.scene_text}",
        f"Shot intent: {request.shot_description}",
        (
            f"Location: {request.location.get('name')}."
            if request.location and request.location.get("name")
            else ""
        ),
        f"Characters: {character_names}." if character_names else "",
        f"Visual references: {assets}." if assets else "",
        camera,
        composition,
        "Keep continuity with the supplied reference image when one is provided.",
    ]
    return "\n".join(section for section in sections if section).strip()
