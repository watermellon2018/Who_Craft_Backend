"""Suggest 3D-editor parameters from a character's reference images.

The autofit endpoint is advisory: it reads the latest READY portrait,
extracts a handful of colors and facial proportions, and returns them in
the same ``{zone_id: {param_id: value}}`` document the editor saves via
the regular ``/model3d`` PUT. Nothing is persisted here — the user reviews
the suggestion in the editor first, so the single existing write path
keeps doing the clamping and revision bookkeeping.

Two quality tiers, decided at runtime:

* mediapipe installed (optional dependency, see requirements.txt): FaceMesh
  landmarks give a real face box, facial proportion metrics and iris color;
* mediapipe missing: a heuristic central crop still yields plausible skin
  and hair colors, and the response carries warnings so the frontend can
  tell the user which sliders were left untouched.

The landmark→parameter mappings are pure functions over normalized point
coordinates, so the geometry stays unit-testable without mediapipe.
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
# Iris centers only exist when FaceMesh runs with refine_landmarks=True
# (478-point topology).
LEFT_IRIS_CENTER = 468
RIGHT_IRIS_CENTER = 473


def compute_autofit(character):
    """Return suggested 3D params for ``character``'s reference images.

    Shape: ``{"params": {...}, "warnings": [...], "sources": {...}}``.
    ``params`` always passes ``validate_model3d_params`` so the frontend
    can hand it straight to the editor; ``warnings`` are short snake_case
    codes explaining which extractions were skipped and why.
    """
    portrait = _latest_ready_asset(character, CharacterAssetType.PORTRAIT)
    full_body = _latest_ready_asset(character, CharacterAssetType.FULL_BODY)
    sources = {
        "portrait": str(portrait.asset_id) if portrait else None,
        "full_body": str(full_body.asset_id) if full_body else None,
    }

    if portrait is None:
        return {"params": {}, "warnings": ["no_portrait"], "sources": sources}

    image = _open_asset_image(portrait)
    if image is None:
        return {
            "params": {},
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

    params = {}
    skin = _median_color(image, _skin_sample_box(face_box))
    if skin:
        params["skin_color"] = {"skinTone": skin}
    hair = _median_color(image, _hair_sample_box(face_box, image.height))
    if hair:
        params["hair"] = {"hairColor": hair}

    if landmarks is None:
        warnings.append("landmarks_unavailable")
        warnings.append("eye_color_unavailable")
    else:
        try:
            metrics = metrics_from_landmarks(landmarks)
        except ValueError:
            # Degenerate geometry (e.g. a sliver of a face at the frame
            # edge) — proportions would be noise, better to leave sliders.
            metrics = {}
            warnings.append("landmarks_unavailable")
        params.update(metrics)
        eye_color = _iris_color(image, landmarks)
        if eye_color:
            params.setdefault("eyes", {})["eyeColor"] = eye_color
        else:
            warnings.append("eye_color_unavailable")

    return {
        "params": validate_model3d_params(params),
        "warnings": warnings,
        "sources": sources,
    }


# ---------------------------------------------------------------------------
# Landmark geometry → editor parameters (pure, mediapipe-free)
# ---------------------------------------------------------------------------


def metrics_from_landmarks(points):
    """Map normalized FaceMesh points to facial-proportion zone params.

    ``points`` is a sequence or mapping of landmark index → ``(x, y)`` in
    normalized image coordinates (y grows downward, as mediapipe emits
    them). Each ratio is compared against a canonical human proportion and
    scaled so that "one scale unit" of deviation fills the editor's
    [-1, 1] slider range. Raises ``ValueError`` when the required points
    are missing or the face box degenerates to a line.
    """
    face_width = _dist(_point(points, FACE_LEFT), _point(points, FACE_RIGHT))
    face_height = _dist(_point(points, FOREHEAD), _point(points, CHIN))
    if face_width < 1e-6 or face_height < 1e-6:
        raise ValueError("degenerate face landmarks")

    eye_gap = _dist(_point(points, LEFT_EYE_INNER), _point(points, RIGHT_EYE_INNER))
    left_eye = _dist(_point(points, LEFT_EYE_OUTER), _point(points, LEFT_EYE_INNER))
    right_eye = _dist(_point(points, RIGHT_EYE_INNER), _point(points, RIGHT_EYE_OUTER))
    nose = _dist(_point(points, NOSE_WING_LEFT), _point(points, NOSE_WING_RIGHT))
    mouth = _dist(_point(points, MOUTH_LEFT), _point(points, MOUTH_RIGHT))
    jaw = _dist(_point(points, JAW_LEFT), _point(points, JAW_RIGHT))

    tilt = statistics.mean((
        _eye_tilt(_point(points, LEFT_EYE_OUTER), _point(points, LEFT_EYE_INNER)),
        _eye_tilt(_point(points, RIGHT_EYE_OUTER), _point(points, RIGHT_EYE_INNER)),
    ))
    eye_size = statistics.mean((left_eye, right_eye)) / face_width
    jaw_ratio = jaw / face_width

    return {
        "eyes": {
            "eyeDistance": _scaled(eye_gap / face_width, 0.26, 0.10),
            "eyeTilt": _clamp(tilt / 0.20),
            "eyeSize": _scaled(eye_size, 0.205, 0.06),
        },
        "nose": {"noseWidth": _scaled(nose / face_width, 0.20, 0.07)},
        "mouth": {"mouthWidth": _scaled(mouth / face_width, 0.35, 0.10)},
        "jaw_chin": {"jawWidth": _scaled(jaw_ratio, 0.78, 0.12)},
        "face_shape": {
            "shape": classify_face_shape(face_height / face_width, jaw_ratio),
        },
    }


def classify_face_shape(height_ratio, jaw_ratio):
    """Bucket face proportions into the editor's shape presets.

    ``height_ratio`` is face_height/face_width, ``jaw_ratio`` is
    jaw_width/face_width. Thresholds are coarse on purpose: the preset
    only seeds the morph, the user fine-tunes from there.
    """
    if height_ratio < 1.32:
        return "round"
    if height_ratio > 1.5 and jaw_ratio < 0.74:
        return "heart"
    if jaw_ratio > 0.84:
        return "square"
    return "oval"


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


def _face_box(image, landmarks):
    """Pixel-space face box from landmarks, else a heuristic central crop."""
    if landmarks:
        xs = [p[0] for p in landmarks]
        ys = [p[1] for p in landmarks]
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
    """Horizontal band just above the face box; top of frame as fallback."""
    left, top, right, bottom = face_box
    band_top = max(0.0, top - 0.25 * (bottom - top))
    if top - band_top < 1.0:
        # Face box touches the frame edge — sample the top of the image.
        return (left, 0.0, right, 0.12 * image_height)
    return (left, band_top, right, top)


def _iris_color(image, landmarks):
    """Median color around the iris centers, or None without refined points."""
    if landmarks is None or len(landmarks) <= RIGHT_IRIS_CENTER:
        return None
    radius = max(2.0, 0.01 * min(image.width, image.height))
    pixels = []
    for index in (LEFT_IRIS_CENTER, RIGHT_IRIS_CENTER):
        x = landmarks[index][0] * image.width
        y = landmarks[index][1] * image.height
        pixels.extend(
            _region_pixels(image, (x - radius, y - radius, x + radius, y + radius))
        )
    return _median_hex(pixels)


def _median_color(image, box):
    """Median RGB of ``box`` as ``"#rrggbb"``, or None for an empty box.

    Median (not mean) so stray highlights, shadows and accessories inside
    the sample window do not tint the result.
    """
    return _median_hex(_region_pixels(image, box))


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
