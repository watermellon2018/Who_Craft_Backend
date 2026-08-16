"""Suggest 3D-editor parameters from a character's reference images.

The autofit pipeline reads the latest READY portrait, profile (or 3/4), and
full-body references. The portrait supplies colors and frontal proportions;
the profile supplies depth-sensitive nose, lip, chin, and face controls. It
returns the same ``{zone_id: {param_id: value}}`` document the 3D editor saves.

Two quality tiers, decided at runtime:

* mediapipe installed (optional dependency, see requirements.txt): FaceMesh
  landmarks give facial proportion metrics, iris color, and a polygon skin
  mask (the face oval minus eyes/brows/mouth) for an accurate skin color;
* mediapipe missing: heuristic portrait regions still yield plausible skin
  and hair colors, while a symmetric eye-pair detector samples iris color
  without introducing another native runtime dependency.

Hair has no per-class segmentation under the pinned mediapipe (that needs a
downloaded Tasks-API model we don't ship), so its color stays a best-effort
band above the face — landmark-anchored when possible — and is flagged with
``hair_segmentation_unavailable``.

The landmark→parameter mappings and the skin-mask geometry are pure
functions over normalized point coordinates, so they stay unit-testable
without mediapipe.
"""

import logging
import math
import statistics
from pathlib import Path

from django.conf import settings
from PIL import Image

from w_craft_back.character_studio.models import (
    CharacterAssetStatus,
    CharacterAssetType,
)
from w_craft_back.character_studio.services.model3d_service import (
    validate_model3d_params,
)

logger = logging.getLogger(__name__)

# Canonical mediapipe FaceMesh landmark indices (468-point topology).
LEFT_EYE_OUTER = 33
LEFT_EYE_INNER = 133
RIGHT_EYE_INNER = 362
RIGHT_EYE_OUTER = 263
MOUTH_LEFT = 61
MOUTH_RIGHT = 291
NOSE_WING_LEFT = 48
NOSE_WING_RIGHT = 278
FACE_LEFT = 234
FACE_RIGHT = 454
CHIN = 152
FOREHEAD = 10
JAW_LEFT = 172
JAW_RIGHT = 397
NOSE_BRIDGE = 168
NOSE_TIP = 1
NOSE_BASE = 2
UPPER_LIP_TOP = 0
UPPER_LIP_INNER = 13
LOWER_LIP_INNER = 14
LOWER_LIP_BOTTOM = 17
JAW_UNDERSIDE = 175
# Iris centers only exist when FaceMesh runs with refine_landmarks=True
# (478-point topology).
LEFT_IRIS_CENTER = 468
RIGHT_IRIS_CENTER = 473
LEFT_IRIS_RING = (469, 470, 471, 472)
RIGHT_IRIS_RING = (474, 475, 476, 477)

# Ordered FaceMesh contour rings (canonical 468-point topology, stable across
# mediapipe releases). Hardcoded as plain tuples so the skin-mask geometry is
# a pure function that never imports mediapipe — same testability contract as
# metrics_from_landmarks. The oval is the outer face boundary; the eye/brow/
# lip rings are carved out of it so the median samples real cheek/forehead
# skin, not eyes, brows, lips or the mouth interior.
FACE_OVAL_RING = (
    10, 109, 67, 103, 54, 21, 162, 127, 234, 93, 132, 58, 172, 136, 150,
    149, 176, 148, 152, 377, 400, 378, 379, 365, 397, 288, 361, 323, 454,
    356, 389, 251, 284, 332, 297, 338,
)
LEFT_EYE_RING = (
    249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388,
    466, 263,
)
RIGHT_EYE_RING = (
    7, 33, 246, 161, 160, 159, 158, 157, 173, 133, 155, 154, 153, 145, 144,
    163,
)
LEFT_EYEBROW_RING = (276, 283, 282, 295, 285, 300, 293, 334, 296, 336)
RIGHT_EYEBROW_RING = (46, 53, 52, 65, 55, 70, 63, 105, 66, 107)
# Outer lip contour — covers the whole mouth (lips + the opening between them).
LIPS_OUTER_RING = (
    0, 267, 269, 270, 409, 291, 375, 321, 405, 314, 17, 84, 181, 91, 146,
    61, 185, 40, 39, 37,
)

# Canonical mediapipe Pose landmark indices (33-point topology). Used to
# read body proportions from the full-body reference.
POSE_LEFT_SHOULDER = 11
POSE_RIGHT_SHOULDER = 12
POSE_LEFT_ELBOW = 13
POSE_RIGHT_ELBOW = 14
POSE_LEFT_WRIST = 15
POSE_RIGHT_WRIST = 16
POSE_LEFT_HIP = 23
POSE_RIGHT_HIP = 24
POSE_LEFT_KNEE = 25
POSE_RIGHT_KNEE = 26
POSE_LEFT_ANKLE = 27
POSE_RIGHT_ANKLE = 28

# Canonical body-proportion ratios, measured as the mean over frontal,
# confidently-detected real full-body references. A subject matching these
# means lands on 0 for every emitted slider (neutral = the model's default).
CANON_SHOULDER_HIP = 1.81   # shoulder width / hip width
CANON_LEG_TORSO = 1.45      # leg length / torso height
CANON_THIGH_CALF = 1.09     # thigh segment / calf segment
CANON_ARM_TORSO = 0.97      # arm length / torso height (noisy: bent arms)
CANON_UPPER_FOREARM = 1.15  # upper-arm segment / forearm segment

# Pose confidence gates. Body metrics are emitted only when the detection is
# trustworthy: key joints visible AND the subject roughly frontal (a turned
# torso skews the width ratios). Arm length is additionally gated on the
# elbows being reasonably straight, since a bent arm shortens its apparent
# length regardless of the real proportions.
POSE_MIN_VISIBILITY = 0.6
POSE_MAX_Z_SPREAD = 0.18    # |z| gap of shoulders/hips; larger = turned
POSE_ARM_GATE_FLOOR = 0.15  # below this elbow-straightness, drop arm length
MODEL3D_AUTOFIT_VERSION = 7


HAIR_COLORS = (
    ("strawberry blonde", "#c98257"),
    ("copper", "#b9653b"),
    ("blonde", "#d5ae72"),
    ("blond", "#d5ae72"),
    ("auburn", "#8b4a2f"),
    ("ginger", "#b9653b"),
    ("red", "#a94f32"),
    ("brown", "#5b3828"),
    ("black", "#1e1a18"),
    ("gray", "#8a8580"),
    ("white", "#d9d5ce"),
)
EYE_COLORS = (
    ("light blue", "#5d9ed1"),
    ("blue", "#3a6ca8"),
    ("green", "#4f7447"),
    ("hazel", "#7a5b32"),
    ("brown", "#5b3828"),
    ("gray", "#7b8791"),
    ("amber", "#9a6b2f"),
)
SKIN_COLORS = (
    ("very light", "#f0d8c0"),
    ("fair", "#f0d8c0"),
    ("light", "#e7c5aa"),
    ("warm", "#dac0a3"),
    ("olive", "#b58a6a"),
    ("tan", "#a87555"),
    ("dark", "#5a3a2a"),
)
CLOTHING_COLORS = (
    ("navy blue", "#263a55"),
    ("navy", "#263a55"),
    ("dark blue", "#2f4663"),
    ("burgundy", "#6b3b3b"),
    ("black", "#232329"),
    ("blue", "#3b5266"),
    ("green", "#3b5a3b"),
    ("beige", "#c9b99b"),
    ("white", "#dedbd2"),
    ("red", "#8a4542"),
)
CLOTHING_TOP_STYLE_KEYWORDS = (
    ("sleeveless", ("sleeveless", "tank top", "camisole", "vest", "без рукав", "майк")),
    (
        "long_sleeve",
        (
            "long sleeve", "hoodie", "sweater", "sweatshirt", "jacket",
            "coat", "turtleneck", "blazer", "длинный рукав", "длинным рукавом",
            "худи", "свитер", "толстовк", "куртк", "пальто",
        ),
    ),
)
CLOTHING_BOTTOM_STYLE_KEYWORDS = (
    (
        "trousers",
        (
            "trousers", "pants", "jeans", "leggings", "slacks", "брюк", "штан",
            "джинс", "леггинс",
        ),
    ),
    ("shorts", ("shorts", "шорт")),
)
BODY_PRESETS = {
    "slim": {
        "torso": {"chestWidth": -0.2, "chestDepth": -0.15},
        "waist": {"waistWidth": -0.25},
        "hips": {"hipsWidth": -0.1},
        "upper_arm": {"volume": -0.2},
        "thigh": {"thighVolume": -0.15},
    },
    "athletic": {
        "shoulders": {"shouldersWidth": 0.25},
        "torso": {"chestWidth": 0.15},
        "waist": {"waistWidth": -0.1},
        "upper_arm": {"volume": 0.2, "definition": 0.35},
        "thigh": {"thighVolume": 0.15},
    },
    "muscular": {
        "shoulders": {"shouldersWidth": 0.45},
        "torso": {"chestWidth": 0.35, "chestDepth": 0.3},
        "upper_arm": {"volume": 0.45, "definition": 0.55},
        "thigh": {"thighVolume": 0.35},
    },
    "heavy": {
        "torso": {"chestWidth": 0.3, "chestDepth": 0.35},
        "waist": {"waistWidth": 0.45},
        "hips": {"hipsWidth": 0.35},
        "upper_arm": {"volume": 0.25},
        "thigh": {"thighVolume": 0.3},
    },
}


def _merge_missing(params, suggestions):
    """Merge character-authored suggestions without replacing existing values."""
    for zone_id, values in suggestions.items():
        zone = params.setdefault(zone_id, {})
        for param_id, value in values.items():
            zone.setdefault(param_id, value)


def _hex_or_named_color(value, palette):
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    is_hex = len(normalized) == 7 and (
        normalized.startswith("#")
        and all(char in "0123456789abcdef" for char in normalized[1:])
    )
    if is_hex:
        return normalized
    return next(
        (color for keyword, color in palette if keyword in normalized),
        None,
    )


def _infer_clothing_styles(value):
    """Infer the closest editable garment silhouettes from authored outfit text."""
    normalized = str(value or "").lower().replace("_", " ").replace("-", " ")
    top_style = "tshirt"
    bottom_style = "shorts"
    for style, keywords in CLOTHING_TOP_STYLE_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            top_style = style
            break
    for style, keywords in CLOTHING_BOTTOM_STYLE_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            bottom_style = style
            break
    return top_style, bottom_style


def _character_authored_params(character):
    """Translate the open character's structured appearance into 3D zones."""
    appearance = getattr(character, "active_appearance", None)
    descriptions = (
        str(getattr(appearance, "source_description", "") or ""),
        str(getattr(appearance, "appearance_prompt", "") or ""),
        str(getattr(character, "short_description", "") or ""),
        str(getattr(character, "clothing_description", "") or ""),
    )
    text = " ".join(filter(None, descriptions)).lower()
    params = {}

    face_shape = str(getattr(appearance, "face_shape", "") or "").lower()
    if face_shape in {"oval", "round", "square", "heart"}:
        params["face_shape"] = {"shape": face_shape}
    for field, palette, zone, param_id in (
        ("skin_tone", SKIN_COLORS, "skin_color", "skinTone"),
        ("eye_color", EYE_COLORS, "eyes", "eyeColor"),
        ("hair_color", HAIR_COLORS, "hair", "hairColor"),
    ):
        authored_value = getattr(appearance, field, "") or text
        color = _hex_or_named_color(authored_value, palette)
        if color:
            params.setdefault(zone, {})[param_id] = color

    hair_length = str(getattr(appearance, "hair_length", "") or "").lower()
    if not hair_length:
        lengths = ("bald", "long", "medium", "short")
        hair_length = next(
            (value for value in lengths if value.replace("_", " ") in text),
            "",
        )
    length_profile = {
        "bald": ("none", 0.0),
        "short": ("default", 0.2),
        "medium": ("bob", 0.6),
        "long": ("long", 0.9),
    }.get(hair_length)
    if length_profile:
        hair = params.setdefault("hair", {})
        hair["hairStyle"], hair["hairLength"] = length_profile
    style_text = f"{getattr(appearance, 'hair_style', '')} {text}".lower()
    for keyword, style in (
        ("ponytail", "ponytail"),
        ("bun", "bun"),
        ("afro", "afro"),
        ("bob", "bob"),
    ):
        if keyword in style_text:
            params.setdefault("hair", {})["hairStyle"] = style
            break
    for keyword in ("curly", "wavy", "straight"):
        if keyword in style_text:
            params.setdefault("hair", {})["hairShape"] = keyword
            break

    body_type = str(getattr(appearance, "body_type", "") or "").lower()
    if not body_type:
        body_type = next((key for key in BODY_PRESETS if key in text), "")
    _merge_missing(params, BODY_PRESETS.get(body_type, {}))
    gender = str(getattr(character, "gender", "") or "").lower()
    if gender in {"female", "girl", "woman"}:
        _merge_missing(
            params,
            {
                "shoulders": {"shouldersWidth": -0.08},
                "hips": {"hipsWidth": 0.12},
            },
        )
    elif gender in {"male", "boy", "man"}:
        _merge_missing(
            params,
            {
                "shoulders": {"shouldersWidth": 0.12},
                "hips": {"hipsWidth": -0.12},
            },
        )

    posture = str(getattr(appearance, "posture", "") or "").lower()
    posture_profile = {
        "slouched": {
            "posturePreset": "slouched",
            "postureStraightness": -0.45,
        },
        "confident": {
            "posturePreset": "confident",
            "postureStraightness": 0.35,
        },
        "elegant": {
            "posturePreset": "elegant",
            "postureStraightness": 0.2,
        },
        "relaxed": {
            "posturePreset": "neutral",
            "postureStraightness": -0.08,
        },
    }.get(posture)
    if posture_profile:
        params["posture"] = posture_profile

    outfit = getattr(character, "active_outfit", None)
    palette = getattr(outfit, "color_palette", None) or []
    colors = [
        color
        for color in palette
        if _hex_or_named_color(color, CLOTHING_COLORS)
    ]
    clothing_text = " ".join(
        str(value or "")
        for value in (
            getattr(character, "clothing_description", ""),
            getattr(outfit, "name", ""),
            getattr(outfit, "description", ""),
            getattr(outfit, "style", ""),
            getattr(outfit, "layers", ""),
        )
    )
    top_style, bottom_style = _infer_clothing_styles(clothing_text)
    if clothing_text.strip() or colors:
        params["clothing_top"] = {"enabled": True, "style": top_style}
        params["clothing_bottom"] = {"enabled": True, "style": bottom_style}

    top_source = colors[0] if colors else clothing_text
    top_color = _hex_or_named_color(top_source, CLOTHING_COLORS)
    if top_color:
        params.setdefault("clothing_top", {}).update(
            {"enabled": True, "color": top_color}
        )
    if len(colors) > 1:
        params.setdefault("clothing_bottom", {}).update(
            {
                "enabled": True,
                "color": _hex_or_named_color(colors[1], CLOTHING_COLORS),
            }
        )
    return params


def compute_autofit(character):
    """Return suggested 3D params for ``character``'s reference images.

    Shape: ``{"params": {...}, "warnings": [...], "sources": {...}}``.
    ``params`` always passes ``validate_model3d_params`` so the frontend
    can hand it straight to the editor; ``warnings`` are short snake_case
    codes explaining which extractions were skipped and why.

    Face widths and colors come from the portrait (FaceMesh); depth-sensitive
    controls come from the profile or three-quarter view; body proportions
    come from the full-body reference (Pose). Stages degrade independently.
    """
    portrait = _latest_ready_asset(character, CharacterAssetType.PORTRAIT)
    profile = _latest_ready_asset(character, CharacterAssetType.PROFILE)
    profile_type = CharacterAssetType.PROFILE
    if profile is None:
        profile = _latest_ready_asset(character, CharacterAssetType.THREE_QUARTER)
        profile_type = CharacterAssetType.THREE_QUARTER
    full_body = _latest_ready_asset(character, CharacterAssetType.FULL_BODY)
    appearance = getattr(character, "active_appearance", None)
    outfit = getattr(character, "active_outfit", None)
    sources = {
        "portrait": str(portrait.asset_id) if portrait else None,
        "profile": str(profile.asset_id) if profile else None,
        "profile_type": profile_type if profile else None,
        "profile_pending": profile is None and character.assets.filter(
            asset_type__in=(
                CharacterAssetType.PROFILE,
                CharacterAssetType.THREE_QUARTER,
            ),
            status=CharacterAssetStatus.GENERATING,
        ).exists(),
        "full_body": str(full_body.asset_id) if full_body else None,
        "appearance": str(appearance.appearance_id) if appearance else None,
        "outfit": str(outfit.outfit_id) if outfit else None,
    }
    params = _character_authored_params(character)

    if portrait is None:
        return {
            "params": validate_model3d_params(params),
            "warnings": ["no_portrait"],
            "sources": sources,
        }

    image = _open_asset_image(portrait)
    if image is None:
        return {
            "params": validate_model3d_params(params),
            "warnings": ["portrait_unreadable"],
            "sources": sources,
        }

    warnings = []
    # Belt and suspenders: _mediapipe_landmarks already guards the detector,
    # but the endpoint is advisory, so even an unexpected failure here must
    # degrade to "landmarks_unavailable" rather than a 500.
    try:
        landmarks = _mediapipe_landmarks(image)
    except Exception:
        logger.warning("autofit: landmark extraction failed", exc_info=True)
        landmarks = None
    face_box = _face_box(image, landmarks)

    # ── Skin colour ──
    # Prefer the landmark mask (face oval minus eyes/brows/mouth): it samples
    # real skin, where the old lower-central box caught lips/shadow/beard.
    # Degrade to that box (with a warning) when there are no landmarks or the
    # mask comes out empty.
    skin = _skin_mask_color(image, landmarks)
    if skin is None:
        warnings.append("skin_segmentation_unavailable")
        skin = _median_color(image, _skin_sample_box(face_box))
    if skin:
        params.setdefault("skin_color", {})["skinTone"] = skin

    # ── Hair colour ──
    # Hair has no real segmentation under the mediapipe pin (would need a
    # downloaded model we don't ship), so this stays heuristic. The landmark-
    # anchored band above the forehead is tighter than the old full-bbox band;
    # fall back to that band when landmarks are missing. Either way the result
    # is a best-effort sample, flagged so the frontend can say so.
    hair_box = None
    if landmarks:
        hair_box = hair_band_box(landmarks, image.width, image.height)
    if hair_box is None:
        warnings.append("hair_segmentation_unavailable")
        hair_box = _hair_sample_box(face_box, image.height)
    hair = _hair_band_color(image, hair_box)
    if hair:
        params.setdefault("hair", {}).setdefault("hairColor", hair)

    if landmarks is None:
        warnings.append("landmarks_unavailable")
        eye_color = _fallback_iris_color(image)
        if eye_color:
            params.setdefault("eyes", {})["eyeColor"] = eye_color
        else:
            warnings.append("eye_color_unavailable")
    else:
        try:
            metrics = metrics_from_landmarks(landmarks)
        except ValueError:
            # Degenerate geometry (e.g. a sliver of a face at the frame
            # edge) — proportions would be noise, better to leave sliders.
            metrics = {}
            warnings.append("landmarks_unavailable")
        for zone_id, values in metrics.items():
            params.setdefault(zone_id, {}).update(values)
        eye_color = _iris_color(image, landmarks)
        if eye_color:
            params.setdefault("eyes", {})["eyeColor"] = eye_color
        else:
            warnings.append("eye_color_unavailable")

    # ── Profile depth from the dedicated side/three-quarter reference ──
    _apply_profile_metrics(profile, profile_type, params, warnings)

    # ── Body proportions from the full-body reference (best effort) ──
    _apply_body_metrics(full_body, params, warnings)
    return {
        "params": validate_model3d_params(params),
        "warnings": warnings,
        "sources": sources,
    }


def _apply_profile_metrics(profile, profile_type, params, warnings):
    """Fold depth-sensitive controls from profile or three-quarter landmarks."""
    if profile is None:
        warnings.append("no_profile_reference")
        return
    image = _open_asset_image(profile)
    if image is None:
        warnings.append("profile_unreadable")
        return
    try:
        landmarks = _mediapipe_landmarks(image)
    except Exception:
        logger.warning("autofit: profile landmark extraction failed", exc_info=True)
        landmarks = None
    if landmarks is None:
        warnings.append("profile_landmarks_unavailable")
        return
    try:
        scale = 1.8 if profile_type == CharacterAssetType.THREE_QUARTER else 1.0
        metrics = profile_metrics_from_landmarks(landmarks, projection_scale=scale)
    except ValueError:
        warnings.append("profile_landmarks_unavailable")
        return
    for zone_id, values in metrics.items():
        params.setdefault(zone_id, {}).update(values)


def _apply_body_metrics(full_body, params, warnings):
    """Fold body-proportion params from the full-body reference into
    ``params`` in place, recording a short warning when each stage is
    skipped. Never raises — the editor works fine without these."""
    if full_body is None:
        warnings.append("no_full_body")
        return
    image = _open_asset_image(full_body)
    if image is None:
        warnings.append("full_body_unreadable")
        return
    try:
        pose = _mediapipe_pose(image)
    except Exception:
        logger.warning("autofit: pose extraction failed", exc_info=True)
        pose = None
    if pose is None:
        warnings.append("body_pose_unavailable")
        return

    ok, _z = pose_confidence(pose)
    if not ok:
        # Turned torso or low visibility — width ratios would be noise.
        warnings.append("body_pose_not_frontal")
        return

    try:
        body_params, body_warnings = body_metrics_from_pose(pose)
    except ValueError:
        warnings.append("body_pose_unavailable")
        return
    for zone, values in body_params.items():
        params.setdefault(zone, {}).update(values)
    warnings.extend(body_warnings)


# ---------------------------------------------------------------------------
# Landmark geometry → editor parameters (pure, mediapipe-free)
# ---------------------------------------------------------------------------


def metrics_from_landmarks(points):
    """Map a frontal FaceMesh detection to the editor's face controls."""
    face_width = _dist(_point(points, FACE_LEFT), _point(points, FACE_RIGHT))
    face_height = _dist(_point(points, FOREHEAD), _point(points, CHIN))
    if face_width < 1e-6 or face_height < 1e-6:
        raise ValueError("degenerate face landmarks")

    eye_gap = _dist(_point(points, LEFT_EYE_INNER), _point(points, RIGHT_EYE_INNER))
    left_eye = _dist(_point(points, LEFT_EYE_OUTER), _point(points, LEFT_EYE_INNER))
    right_eye = _dist(_point(points, RIGHT_EYE_INNER), _point(points, RIGHT_EYE_OUTER))
    nose_width = _dist(_point(points, NOSE_WING_LEFT), _point(points, NOSE_WING_RIGHT))
    nose_length = _dist(_point(points, NOSE_BRIDGE), _point(points, NOSE_BASE))
    mouth_width = _dist(_point(points, MOUTH_LEFT), _point(points, MOUTH_RIGHT))
    upper_lip = _dist(_point(points, UPPER_LIP_TOP), _point(points, UPPER_LIP_INNER))
    lower_lip = _dist(_point(points, LOWER_LIP_INNER), _point(points, LOWER_LIP_BOTTOM))
    chin_length = _dist(_point(points, LOWER_LIP_BOTTOM), _point(points, CHIN))
    jaw = _dist(_point(points, JAW_LEFT), _point(points, JAW_RIGHT))

    tilt = statistics.mean((
        _eye_tilt(_point(points, LEFT_EYE_OUTER), _point(points, LEFT_EYE_INNER)),
        _eye_tilt(_point(points, RIGHT_EYE_OUTER), _point(points, RIGHT_EYE_INNER)),
    ))
    eye_size = statistics.mean((left_eye, right_eye)) / face_width
    jaw_ratio = jaw / face_width
    mouth_center_y = statistics.mean((
        _point(points, UPPER_LIP_INNER)[1],
        _point(points, LOWER_LIP_INNER)[1],
    ))
    corner_y = statistics.mean((
        _point(points, MOUTH_LEFT)[1],
        _point(points, MOUTH_RIGHT)[1],
    ))
    corner_lift = (mouth_center_y - corner_y) / max(mouth_width, 1e-6)

    return {
        "eyes": {
            "eyeDistance": _scaled(eye_gap / face_width, 0.26, 0.10),
            "eyeTilt": _clamp(tilt / 0.20),
            "eyeSize": _scaled(eye_size, 0.205, 0.06),
        },
        "nose": {
            "noseLength": _scaled(nose_length / face_height, 0.32, 0.08),
            "noseWidth": _scaled(nose_width / face_width, 0.20, 0.07),
        },
        "mouth": {
            "mouthWidth": _scaled(mouth_width / face_width, 0.35, 0.10),
            "upperLip": _scaled(upper_lip / face_height, 0.025, 0.018),
            "lowerLip": _scaled(lower_lip / face_height, 0.040, 0.022),
            "cornerLift": _scaled(corner_lift, 0.02, 0.10),
        },
        "jaw_chin": {
            "jawWidth": _scaled(jaw_ratio, 0.78, 0.12),
            "chinLength": _scaled(chin_length / face_height, 0.19, 0.06),
        },
        "face_shape": {
            "shape": classify_face_shape(face_height / face_width, jaw_ratio),
        },
    }


def _signed_line_projection(point, line_start, line_end):
    """Perpendicular distance to a 2D line, normalized by line length."""
    dx = line_end[0] - line_start[0]
    dy = line_end[1] - line_start[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        raise ValueError("degenerate profile landmarks")
    return (
        (point[0] - line_start[0]) * (-dy)
        + (point[1] - line_start[1]) * dx
    ) / (length * length)


def profile_metrics_from_landmarks(points, projection_scale=1.0):
    """Map profile/three-quarter landmarks to depth-sensitive face controls.

    The sign is deliberately discarded: generated references may face left or
    right. ``projection_scale`` compensates a known 30-degree three-quarter
    view; an exact profile uses the default 1.0.
    """
    forehead = _point(points, FOREHEAD)
    chin = _point(points, CHIN)
    face_height = _dist(forehead, chin)
    if face_height < 1e-6:
        raise ValueError("degenerate profile landmarks")

    def projection(index):
        value = _signed_line_projection(_point(points, index), forehead, chin)
        return abs(value) * projection_scale

    nose_projection = projection(NOSE_TIP)
    bridge_projection = projection(NOSE_BRIDGE)
    upper_lip_projection = projection(UPPER_LIP_INNER)
    lower_lip_projection = projection(LOWER_LIP_INNER)
    side_a = _signed_line_projection(_point(points, FACE_LEFT), forehead, chin)
    side_b = _signed_line_projection(_point(points, FACE_RIGHT), forehead, chin)
    nose_signed = _signed_line_projection(_point(points, NOSE_TIP), forehead, chin)
    profile_span = max(side_a, side_b, nose_signed) - min(
        side_a, side_b, nose_signed,
    )
    face_depth = profile_span * projection_scale
    chin_projection = abs(_signed_line_projection(
        _point(points, CHIN),
        forehead,
        _point(points, JAW_UNDERSIDE),
    )) * projection_scale

    return {
        "face_shape": {"faceDepth": _scaled(face_depth, 1.25, 0.30)},
        "nose": {
            "noseTip": _scaled(nose_projection, 0.24, 0.10),
            "bridgeHeight": _scaled(bridge_projection, 0.045, 0.030),
        },
        "mouth": {
            "upperLip": _scaled(upper_lip_projection, 0.075, 0.050),
            "lowerLip": _scaled(lower_lip_projection, 0.075, 0.050),
        },
        "jaw_chin": {"chinShape": _scaled(chin_projection, 0.040, 0.030)},
    }


def classify_face_shape(height_ratio, jaw_ratio):
    """Bucket real FaceMesh height/width and jaw/width ratios."""
    if height_ratio < 0.64:
        return "round"
    if height_ratio > 0.76 and jaw_ratio < 0.74:
        return "heart"
    if jaw_ratio > 0.84:
        return "square"
    return "oval"


def pose_confidence(points):
    """Return ``(ok, z_spread)`` for the body-pose detection.

    ``ok`` is False when key joints are barely visible or the subject is
    clearly turned (a non-frontal torso makes the width ratios meaningless).
    Pure over the landmark mapping so the gate is unit-testable.
    """
    needed = (
        POSE_LEFT_SHOULDER, POSE_RIGHT_SHOULDER,
        POSE_LEFT_HIP, POSE_RIGHT_HIP,
    )
    visibilities = []
    for idx in needed:
        p = _point(points, idx)
        # visibility is the 4th component when present; absent in synthetic
        # test fixtures, which are treated as fully visible.
        visibilities.append(p[3] if len(p) > 3 else 1.0)
    if min(visibilities) < POSE_MIN_VISIBILITY:
        return False, None

    ls, rs = _point(points, POSE_LEFT_SHOULDER), _point(points, POSE_RIGHT_SHOULDER)
    lh, rh = _point(points, POSE_LEFT_HIP), _point(points, POSE_RIGHT_HIP)
    z = lambda p: p[2] if len(p) > 2 else 0.0  # noqa: E731
    z_spread = max(abs(z(ls) - z(rs)), abs(z(lh) - z(rh)))
    return z_spread <= POSE_MAX_Z_SPREAD, z_spread


def body_metrics_from_pose(points):
    """Map normalized Pose landmarks to body-proportion zone params.

    Only scale-free landmark-distance RATIOS are used (never pixel-absolute
    widths), because the photo's crop/distance is unknown. Every ratio is
    compared to a canonical mean so a typical subject lands on 0. Returns
    ``({zone: {param: value}}, warnings)``; arm length is dropped (with an
    ``arm_length_unavailable`` warning) when both elbows are too bent for the
    apparent arm length to be trustworthy.

    The caller is responsible for the confidence gate (pose_confidence);
    this function assumes a usable, frontal detection. Raises ``ValueError``
    on degenerate geometry (zero-length torso/limbs).
    """
    warnings = []

    ls = _point(points, POSE_LEFT_SHOULDER)
    rs = _point(points, POSE_RIGHT_SHOULDER)
    lh = _point(points, POSE_LEFT_HIP)
    rh = _point(points, POSE_RIGHT_HIP)

    shoulder_w = _dist(ls, rs)
    hip_w = _dist(lh, rh)
    mid_shoulder = ((ls[0] + rs[0]) / 2, (ls[1] + rs[1]) / 2)
    mid_hip = ((lh[0] + rh[0]) / 2, (lh[1] + rh[1]) / 2)
    torso_h = _dist(mid_shoulder, mid_hip)
    if hip_w < 1e-6 or torso_h < 1e-6 or shoulder_w < 1e-6:
        raise ValueError("degenerate pose geometry")

    # ── Silhouette: one shoulder/hip deviation fans out to four sliders ──
    sil = _scaled(shoulder_w / hip_w, CANON_SHOULDER_HIP, 0.5)
    params = {
        "shoulders": {"shouldersWidth": _clamp(0.6 * sil)},
        "hips": {"hipsWidth": _clamp(-0.45 * sil)},
        "waist": {
            "waistWidth": _clamp(-0.35 * sil),
            "torsoCurve": _clamp(-0.5 * sil),
        },
    }

    # ── Legs: overall leg/torso length, split by the thigh/calf ratio ──
    l_thigh = _dist(lh, _point(points, POSE_LEFT_KNEE))
    l_calf = _dist(_point(points, POSE_LEFT_KNEE), _point(points, POSE_LEFT_ANKLE))
    if l_thigh > 1e-6 and l_calf > 1e-6:
        leg_len = (l_thigh + l_calf)
        leg_sig = _scaled(leg_len / torso_h, CANON_LEG_TORSO, 0.5)
        seg_dev = (l_thigh / l_calf - CANON_THIGH_CALF) / 0.5
        params["thigh"] = {"thighLength": _clamp(0.6 * leg_sig + 0.5 * seg_dev)}
        params["calf"] = {"calfLength": _clamp(0.6 * leg_sig - 0.5 * seg_dev)}

    # ── Arms: gated on elbow straightness (bent arms read short) ──
    l_upper = _dist(ls, _point(points, POSE_LEFT_ELBOW))
    l_fore = _dist(_point(points, POSE_LEFT_ELBOW), _point(points, POSE_LEFT_WRIST))
    g = _arm_straightness(points)
    if l_upper > 1e-6 and l_fore > 1e-6 and g >= POSE_ARM_GATE_FLOOR:
        arm_len = (l_upper + l_fore)
        arm_sig = _scaled(arm_len / torso_h, CANON_ARM_TORSO, 0.7) * g
        ua_dev = (l_upper / l_fore - CANON_UPPER_FOREARM) / 0.5
        params["upper_arm"] = {"length": _clamp(0.55 * arm_sig + 0.5 * ua_dev * g)}
        params["forearm"] = {"length": _clamp(0.55 * arm_sig - 0.5 * ua_dev * g)}
    else:
        warnings.append("arm_length_unavailable")

    return params, warnings


def _arm_straightness(points):
    """Mean elbow-straightness gate in [0, 1]: 1 when arms are straight, 0
    when bent past ~127°. Averaged over both elbows so one bent arm still
    leaves a usable signal from the other."""
    gates = []
    for sh, el, wr in (
        (POSE_LEFT_SHOULDER, POSE_LEFT_ELBOW, POSE_LEFT_WRIST),
        (POSE_RIGHT_SHOULDER, POSE_RIGHT_ELBOW, POSE_RIGHT_WRIST),
    ):
        try:
            cos_a = _cos_angle(
                _point(points, el), _point(points, sh), _point(points, wr),
            )
        except ValueError:
            continue
        gates.append(max(0.0, min(1.0, (cos_a - 0.6) / 0.4)))
    return sum(gates) / len(gates) if gates else 0.0


def _cos_angle(vertex, a, b):
    """Cosine of the angle at ``vertex`` between vertex→a and vertex→b. 1 =
    straight (collinear, a and b on opposite sides), −1 = folded back."""
    ax, ay = a[0] - vertex[0], a[1] - vertex[1]
    bx, by = b[0] - vertex[0], b[1] - vertex[1]
    na = math.hypot(ax, ay)
    nb = math.hypot(bx, by)
    if na < 1e-9 or nb < 1e-9:
        raise ValueError("degenerate angle")
    # vertex→shoulder and vertex→wrist point in OPPOSITE directions for a
    # straight arm, so their dot is negative; negate to make straight = +1.
    return -(ax * bx + ay * by) / (na * nb)


def _point(points, index):
    try:
        return points[index]
    except (KeyError, IndexError):
        raise ValueError(f"missing landmark {index}")


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _eye_tilt(outer, inner):
    # Image y grows downward, so an outer corner that sits HIGHER than the
    # inner corner has a smaller y — that must come out positive.
    return math.atan2(inner[1] - outer[1], abs(inner[0] - outer[0]))


def _clamp(value):
    return max(-1.0, min(1.0, value))


def _scaled(ratio, canonical, scale):
    return _clamp((ratio - canonical) / scale)


# ---------------------------------------------------------------------------
# Skin-mask geometry (pure, mediapipe-free)
#
# Builds a polygon mask of real facial skin from the FaceMesh contour rings —
# the face oval minus the eyes, brows and mouth — and enumerates the pixel
# coordinates inside it. The colour median is then taken over those exact
# pixels (in the image section below), which is far less prone to lips,
# shadows or beard than the old fixed lower-central box. Everything here is a
# pure function over coordinate tuples so it unit-tests without mediapipe.
# ---------------------------------------------------------------------------


def _ring_polygon(points, ring, width, height):
    """Pixel-space polygon for a contour ``ring`` of landmark indices.

    Returns ``[(x, y), ...]`` in pixels, or ``None`` if any vertex is
    missing (e.g. iris-refined points absent) — the caller then degrades.
    """
    polygon = []
    for index in ring:
        try:
            p = _point(points, index)
        except ValueError:
            return None
        polygon.append((p[0] * width, p[1] * height))
    return polygon


def point_in_polygon(x, y, polygon):
    """Ray-casting point-in-polygon test. ``polygon`` is a list of pixel
    vertices; the boundary is treated as inside-ish (good enough for a
    sampling mask). Pure and unit-testable."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Does the horizontal ray at y cross the edge (i, j)?
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x <= x_cross:
                inside = not inside
        j = i
    return inside


def _polygon_bbox(polygon):
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def skin_mask_sample_points(points, width, height, max_samples=600):
    """Integer pixel coords of real facial skin, by landmark mask.

    The mask is the face oval minus the eyes, brows and mouth. Points are
    taken on a regular grid over the oval's bounding box (grid step chosen so
    the count stays near ``max_samples``), keeping only those inside the oval
    and outside every excluded hole.

    Returns ``[]`` when the oval ring is unavailable or degenerate, so the
    caller can fall back to the old box heuristic. Pure: no image, no
    mediapipe.
    """
    if not points or width <= 0 or height <= 0:
        return []
    oval = _ring_polygon(points, FACE_OVAL_RING, width, height)
    if oval is None:
        return []
    left, top, right, bottom = _polygon_bbox(oval)
    box_w = right - left
    box_h = bottom - top
    if box_w < 2 or box_h < 2:
        return []

    # Holes are optional: if a ring is missing we simply skip excluding it
    # rather than abandoning the whole mask (the oval alone is still a big
    # improvement over the box).
    holes = []
    for ring in (
        LEFT_EYE_RING, RIGHT_EYE_RING,
        LEFT_EYEBROW_RING, RIGHT_EYEBROW_RING,
        LIPS_OUTER_RING,
    ):
        hole = _ring_polygon(points, ring, width, height)
        if hole is not None:
            holes.append(hole)

    # Pick a grid step that yields roughly max_samples candidate cells over
    # the bbox; the in-oval keep-rate is ~0.6, so the kept count lands below
    # max_samples without an explicit second pass.
    cells = max(1, int(math.sqrt(max_samples)))
    step_x = max(1, int(box_w / cells))
    step_y = max(1, int(box_h / cells))

    samples = []
    y = int(top)
    end_y = int(bottom)
    end_x = int(right)
    while y <= end_y:
        x = int(left)
        while x <= end_x:
            if point_in_polygon(x, y, oval) and not any(
                point_in_polygon(x, y, hole) for hole in holes
            ):
                samples.append((x, y))
            x += step_x
        y += step_y
    return samples


# ---------------------------------------------------------------------------
# Image access and color sampling
# ---------------------------------------------------------------------------


def _latest_ready_asset(character, asset_type):
    return (
        character.assets.filter(
            asset_type=asset_type,
            status=CharacterAssetStatus.READY,
        )
        .order_by("-version", "-created_at")
        .first()
    )


def _open_asset_image(asset):
    """Open the asset from MEDIA_ROOT as RGB, or None when unreadable."""
    if not asset.storage_path:
        return None
    path = Path(settings.MEDIA_ROOT) / asset.storage_path
    try:
        with Image.open(path) as image:
            return image.convert("RGB")
    except (OSError, ValueError):
        # A dangling row (file pruned from disk, truncated upload) should
        # degrade to a warning, not a 500 — the editor works fine without
        # the suggestion.
        logger.warning("autofit: cannot read portrait asset %s", asset.asset_id)
        return None


def _mediapipe_landmarks(image):
    """Normalized FaceMesh points, or None when mediapipe can't help.

    mediapipe is intentionally NOT a hard dependency (heavy native wheel,
    unavailable on some deploy targets), so the import failure is a normal
    code path, not an error.
    """
    try:
        import mediapipe  # noqa: F401  (optional, see requirements.txt)
        import numpy
    except ImportError:
        return None

    # The native FaceMesh graph can raise at runtime (RuntimeError/ValueError
    # from the C++ calculators on odd image shapes or internal failures). The
    # endpoint is advisory, so a detector failure must degrade to the
    # "landmarks_unavailable" warning, never a 500 — same contract as
    # _open_asset_image. The `with` block still closes FaceMesh on exception.
    try:
        face_mesh = mediapipe.solutions.face_mesh.FaceMesh(
            static_image_mode=True,
            max_num_faces=1,
            refine_landmarks=True,
        )
        with face_mesh:
            results = face_mesh.process(numpy.asarray(image))
    except Exception:
        logger.warning("autofit: mediapipe face detection failed", exc_info=True)
        return None
    if not getattr(results, "multi_face_landmarks", None):
        return None
    return [(lm.x, lm.y) for lm in results.multi_face_landmarks[0].landmark]


def _mediapipe_pose(image):
    """Normalized Pose landmarks ``[(x, y, z, visibility), ...]`` or None.

    Same optional-dependency / degrade-to-None contract as
    _mediapipe_landmarks. model_complexity=2 (the heavy model) is worth it
    here: this runs once per character on the first 3D open, and the extra
    accuracy directly improves the proportion estimate.
    """
    try:
        import mediapipe  # noqa: F401  (optional, see requirements.txt)
        import numpy
    except ImportError:
        return None

    try:
        pose = mediapipe.solutions.pose.Pose(
            static_image_mode=True,
            model_complexity=2,
        )
        with pose:
            results = pose.process(numpy.asarray(image))
    except Exception:
        logger.warning("autofit: mediapipe pose detection failed", exc_info=True)
        return None
    if not getattr(results, "pose_landmarks", None):
        return None
    return [
        (lm.x, lm.y, lm.z, lm.visibility)
        for lm in results.pose_landmarks.landmark
    ]


def _face_box(image, landmarks):
    """Pixel-space face box from landmarks, else a heuristic central crop."""
    if landmarks:
        values = landmarks.values() if hasattr(landmarks, "values") else landmarks
        values = list(values)
        xs = [point[0] for point in values]
        ys = [point[1] for point in values]
        return (
            min(xs) * image.width,
            min(ys) * image.height,
            max(xs) * image.width,
            max(ys) * image.height,
        )
    # No detector: portraits are framed with the face roughly centered, so
    # a fixed central window is a serviceable stand-in.
    return (
        0.30 * image.width,
        0.25 * image.height,
        0.70 * image.width,
        0.75 * image.height,
    )


def _skin_sample_box(face_box):
    """Lower-central area of the face box: cheeks/chin, away from eyes."""
    left, top, right, bottom = face_box
    width = right - left
    height = bottom - top
    return (
        left + 0.30 * width,
        top + 0.55 * height,
        right - 0.30 * width,
        top + 0.90 * height,
    )


def _hair_sample_box(face_box, image_height):
    """Sample the crown above the forehead; use the frame top as fallback."""
    left, top, right, bottom = face_box
    face_height = bottom - top
    band_top = max(0.0, top - 0.40 * face_height)
    band_bottom = max(0.0, top - 0.14 * face_height)
    if band_bottom - band_top < 1.0:
        # Face box touches the frame edge — sample the top of the image.
        return (left, 0.0, right, 0.12 * image_height)
    return (left, band_top, right, band_bottom)


def hair_band_box(points, width, height):
    """Tighter hair sample box anchored on FaceMesh landmarks, or None.

    The honest answer under the mediapipe 0.10.14 pin: hair cannot be
    segmented as its own class without a downloaded Tasks-API model (which we
    won't ship). So we keep the above-the-face heuristic but anchor it to the
    landmarks instead of the full point bbox — a band just above the forehead
    point (10), clamped to the skull width (face sides 234↔454) and centred on
    the face. That trims the ears/shoulders/background the old full-bbox band
    caught, without pretending to be real segmentation.

    Returns a pixel box ``(left, top, right, bottom)`` or None when the
    needed points are missing or the band would have no height (forehead at
    the very top of the frame) — the caller then falls back to the box/​warn.
    Pure: coordinates in, box out, no image and no mediapipe.
    """
    try:
        forehead = _point(points, FOREHEAD)
        face_left = _point(points, FACE_LEFT)
        face_right = _point(points, FACE_RIGHT)
        chin = _point(points, CHIN)
    except ValueError:
        return None

    fy = forehead[1] * height
    face_h = (chin[1] - forehead[1]) * height
    if face_h <= 1.0:
        return None
    # Band sits above the forehead; its height scales with the face so it
    # adapts to crop/zoom. Clamp to the top of the image.
    band_h = 0.32 * face_h
    top = fy - band_h
    if top < 0:
        top = 0.0
    bottom = fy
    if bottom - top < 1.0:
        # Forehead too close to the frame top — nothing reliable above it.
        return None

    lx = face_left[0] * width
    rx = face_right[0] * width
    if rx < lx:
        lx, rx = rx, lx
    skull_w = rx - lx
    # Pull the sides in slightly: the temples already curve toward hair, so
    # the central skull width tracks the crown better than the full width.
    inset = 0.12 * skull_w
    left = lx + inset
    right = rx - inset
    if right - left < 1.0:
        return None
    return (left, top, right, bottom)


def _fallback_iris_color(image):
    """Sample paired irises when FaceMesh is unavailable.

    Generated portraits keep both eyes in a predictable upper-center band. A
    small symmetric contrast search finds dark iris centers surrounded by
    lighter sclera, then the dominant chromatic pixels provide the color. The
    confidence gate rejects flat skin/hair illustrations instead of inventing
    a color.
    """
    size = 192
    resized = image.convert("RGB").resize(
        (size, size),
        Image.Resampling.BILINEAR,
    )
    pixels = list(resized.getdata())
    luminance = [
        0.2126 * red + 0.7152 * green + 0.0722 * blue
        for red, green, blue in pixels
    ]
    integral = [[0.0] * (size + 1) for _ in range(size + 1)]
    for y in range(size):
        row_sum = 0.0
        previous = integral[y]
        current = integral[y + 1]
        for x in range(size):
            row_sum += luminance[y * size + x]
            current[x + 1] = previous[x + 1] + row_sum

    def box_mean(center_x, center_y, radius):
        left = max(0, center_x - radius)
        top = max(0, center_y - radius)
        right = min(size, center_x + radius + 1)
        bottom = min(size, center_y + radius + 1)
        total = (
            integral[bottom][right]
            - integral[top][right]
            - integral[bottom][left]
            + integral[top][left]
        )
        return total / max(1, (right - left) * (bottom - top))

    best = None
    sample_radius = 2
    for center_y in range(round(size * 0.20), round(size * 0.34)):
        for face_center_x in range(round(size * 0.46), round(size * 0.55)):
            for eye_offset in range(round(size * 0.08), round(size * 0.16)):
                centers = (
                    face_center_x - eye_offset,
                    face_center_x + eye_offset,
                )
                contrasts = []
                for center_x in centers:
                    iris = box_mean(center_x, center_y, sample_radius)
                    sclera = statistics.mean((
                        box_mean(center_x - 6, center_y, sample_radius),
                        box_mean(center_x + 6, center_y, sample_radius),
                    ))
                    contrasts.append(sclera - iris)
                score = (
                    statistics.mean(contrasts)
                    - abs(contrasts[0] - contrasts[1]) * 0.45
                    - eye_offset * 0.05
                )
                if best is None or score > best[0]:
                    best = (score, centers, center_y)

    if best is None or best[0] < 12.0:
        return None

    _, centers, center_y = best
    iris_pixels = []
    iris_radius = 3
    for center_x in centers:
        for y in range(center_y - iris_radius, center_y + iris_radius + 1):
            for x in range(center_x - iris_radius, center_x + iris_radius + 1):
                if (x - center_x) ** 2 + (y - center_y) ** 2 <= iris_radius ** 2:
                    iris_pixels.append(resized.getpixel((x, y)))

    categories = {"cool": [], "green": [], "warm": []}
    for pixel in iris_pixels:
        red, green, blue = pixel
        brightness = (red + green + blue) / 3
        if not 20 < brightness < 205:
            continue
        if blue - red > 12 and blue - green > 5:
            categories["cool"].append(pixel)
        elif green - red > 8 and green - blue > 3:
            categories["green"].append(pixel)
        elif red - blue > 12 and red - green > 5:
            categories["warm"].append(pixel)

    category, candidates = max(categories.items(), key=lambda item: len(item[1]))
    if len(candidates) < 4:
        return None
    ranked = sorted(
        candidates,
        key=lambda pixel: (
            0.2126 * pixel[0] + 0.7152 * pixel[1] + 0.0722 * pixel[2]
        ),
    )
    low_fraction, high_fraction = (
        (0.25, 0.70) if category == "warm" else (0.45, 0.90)
    )
    low = int((len(ranked) - 1) * low_fraction)
    high = int((len(ranked) - 1) * high_fraction)
    selected = ranked[low:high + 1]
    color = tuple(
        round(statistics.median(pixel[channel] for pixel in selected))
        for channel in range(3)
    )
    return "#{:02x}{:02x}{:02x}".format(*color)


def _iris_color(image, landmarks):
    """Median iris color with the dark pupil and bright highlights excluded."""
    iris_specs = (
        (LEFT_IRIS_CENTER, LEFT_IRIS_RING),
        (RIGHT_IRIS_CENTER, RIGHT_IRIS_RING),
    )
    last_index = max(index for _, ring in iris_specs for index in ring)
    if landmarks is None or len(landmarks) <= last_index:
        return None

    pixels = []
    for center_index, ring in iris_specs:
        center_x = landmarks[center_index][0] * image.width
        center_y = landmarks[center_index][1] * image.height
        radius_x = max(
            abs(landmarks[index][0] * image.width - center_x)
            for index in ring
        )
        radius_y = max(
            abs(landmarks[index][1] * image.height - center_y)
            for index in ring
        )
        if radius_x < 1.0 or radius_y < 1.0:
            continue

        left = max(0, int(center_x - radius_x))
        right = min(image.width, int(center_x + radius_x) + 1)
        top = max(0, int(center_y - radius_y))
        bottom = min(image.height, int(center_y + radius_y) + 1)
        for y in range(top, bottom):
            for x in range(left, right):
                normalized_radius = (
                    ((x - center_x) / radius_x) ** 2
                    + ((y - center_y) / radius_y) ** 2
                )
                if 0.18 <= normalized_radius <= 0.92:
                    pixels.append(image.getpixel((x, y)))

    if not pixels:
        return None

    # Stylized portraits often devote most center pixels to a near-black pupil.
    # Keep the middle-brightness part of the iris annulus so pupil/eyelashes and
    # sclera/highlights cannot dominate the median.
    ranked = sorted((
        0.2126 * pixel[0] + 0.7152 * pixel[1] + 0.0722 * pixel[2],
        pixel,
    ) for pixel in pixels)
    low = ranked[int((len(ranked) - 1) * 0.45)][0]
    high = ranked[int((len(ranked) - 1) * 0.85)][0]
    return _median_hex([
        pixel for luminance, pixel in ranked if low <= luminance <= high
    ])


def _hair_band_color(image, box):
    """Hair color from the darker middle of a highlight-heavy crown band."""
    pixels = _region_pixels(image, box)
    if not pixels:
        return None
    ranked = sorted((
        0.2126 * pixel[0] + 0.7152 * pixel[1] + 0.0722 * pixel[2],
        pixel,
    ) for pixel in pixels)
    low = ranked[int((len(ranked) - 1) * 0.10)][0]
    high = ranked[int((len(ranked) - 1) * 0.60)][0]
    return _median_hex([
        pixel for luminance, pixel in ranked if low <= luminance <= high
    ])


def _median_color(image, box):
    """Median RGB of ``box`` as ``"#rrggbb"``, or None for an empty box.

    Median (not mean) so stray highlights, shadows and accessories inside
    the sample window do not tint the result.
    """
    return _median_hex(_region_pixels(image, box))


def _skin_mask_color(image, landmarks):
    """Median skin colour over the landmark mask, or None when unavailable.

    Samples the exact face-oval-minus-features pixels rather than a blind
    box, so lips, shadows and beard inside the old window no longer tint the
    result. Returns None (caller falls back to the box) when there are no
    landmarks or the mask comes out degenerate/empty.
    """
    if not landmarks:
        return None
    points = skin_mask_sample_points(landmarks, image.width, image.height)
    if not points:
        return None
    pixels = [image.getpixel((x, y)) for (x, y) in points]
    return _median_hex(pixels)


def _region_pixels(image, box):
    left = max(0, int(box[0]))
    top = max(0, int(box[1]))
    right = min(image.width, int(box[2]))
    bottom = min(image.height, int(box[3]))
    if right <= left or bottom <= top:
        return []
    region = image.crop((left, top, right, bottom))
    if region.width > 96 or region.height > 96:
        # Medians survive resampling; shrinking keeps huge uploads cheap.
        region.thumbnail((96, 96))
    return list(region.getdata())


def _median_hex(pixels):
    if not pixels:
        return None
    channels = (
        int(statistics.median(p[0] for p in pixels)),
        int(statistics.median(p[1] for p in pixels)),
        int(statistics.median(p[2] for p in pixels)),
    )
    return "#{:02x}{:02x}{:02x}".format(*channels)
