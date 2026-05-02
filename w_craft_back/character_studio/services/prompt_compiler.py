import logging

from w_craft_back.character_studio.constants import VISUAL_STYLES

logger = logging.getLogger(__name__)

NEGATIVE_BASE = (
    "different person, changed face, changed eyes, changed outfit, changed age, "
    "distorted face, extra limbs"
)


# Stronger portrait instruction used specifically for initial selection variants.
INITIAL_PORTRAIT_FRAME = (
    "HEAD AND SHOULDERS PORTRAIT ONLY. "
    "Tight portrait composition: face, neck, and upper shoulders clearly visible. "
    "Character centered in frame. Clear, readable facial expression. "
    "DO NOT show full body. DO NOT show legs or torso below upper chest. "
    "DO NOT use scenic or cinematic background composition. "
    "DO NOT use reference-sheet layout or multiple poses. "
    "Focus entirely on the character face and upper-body portrait."
)

# When generating N selection variants, variation is ONLY allowed in these aspects.
PORTRAIT_VARIATION_GUIDE = (
    "Variation between variants is allowed ONLY in: "
    "portrait angle (front-facing, slight 3/4 left, slight 3/4 right), "
    "facial expression (neutral, slight smile, focused), "
    "lighting mood (soft, dramatic, diffused). "
    "All other attributes MUST remain identical across every variant."
)

# Negative prompt for initial portrait selection – stricter than the general base.
INITIAL_PORTRAIT_NEGATIVE = (
    "full body, full length, legs visible, wide shot, low-angle shot showing torso, "
    "scene composition, environmental storytelling, landscape background, "
    "reference sheet, character sheet, multiple poses in one image, silhouette only, "
    "different hair color, different eye color, changed age, changed gender, "
    "different person, distorted face, extra limbs, blurry face"
)

# Anti-text / anti-card negative used for portrait and full_body.
CLEAN_FRAME_NEGATIVE = (
    "text, letters, words, captions, labels, typography, watermark, logo, "
    "UI elements, interface, character card, stats panel, information sheet, "
    "infographic, table, document, poster, comic panel, speech bubbles, "
    "multiple characters, second character, background scene, environment, "
    "extra objects"
)

NEGATIVES_BY_IMAGE_TYPE = {
    "portrait": f"{NEGATIVE_BASE}, {CLEAN_FRAME_NEGATIVE}",
    "full_body": f"{NEGATIVE_BASE}, {CLEAN_FRAME_NEGATIVE}",
    "scene": NEGATIVE_BASE,
    "reference_sheet": NEGATIVE_BASE,
}


# --- Enum → natural-language phrase tables ---------------------------------
# Each phrase is a noun phrase that fits inside a sentence WITHOUT a "label:"
# prefix. This is what stops the model from rendering character cards.

FACE_SHAPE_PHRASE = {
    "oval": "an oval face",
    "round": "a round face",
    "square": "a square jawline and angular face",
    "heart": "a heart-shaped face",
    "long": "a long face",
    "diamond": "a diamond-shaped face",
}

EYE_SHAPE_PHRASE = {
    "almond": "almond-shaped eyes",
    "round": "round eyes",
    "narrow": "narrow eyes",
    "hooded": "hooded eyes",
    "upturned": "upturned eyes",
    "downturned": "downturned eyes",
}

EYEBROW_SHAPE_PHRASE = {
    "thin": "thin eyebrows",
    "thick": "thick eyebrows",
    "straight": "straight eyebrows",
    "arched": "arched eyebrows",
    "soft": "soft eyebrows",
    "sharp": "sharp eyebrows",
}

NOSE_SHAPE_PHRASE = {
    "straight": "a straight nose",
    "button": "a small button nose",
    "aquiline": "an aquiline nose",
    "wide": "a wide nose",
    "narrow": "a narrow nose",
    "flat": "a flat nose",
    "sharp": "a sharp nose",
}

LIPS_SHAPE_PHRASE = {
    "thin": "thin lips",
    "medium": "medium lips",
    "full": "full lips",
    "sharp": "sharply defined lips",
    "soft": "soft lips",
}

HAIR_LENGTH_PHRASE = {
    "short": "short hair",
    "medium": "medium-length hair",
    "long": "long hair",
    "shaved": "a shaved head",
    "bald": "a bald head",
}

BODY_TYPE_PHRASE = {
    "slim": "a slim build",
    "athletic": "an athletic build",
    "muscular": "a muscular build",
    "average": "an average build",
    "heavy": "a heavy build",
}

DISTINCTIVE_FEATURE_PHRASE = {
    "freckles": "freckles across the face",
    "mole": "a small mole",
    "scar": "a visible scar",
    "birthmark": "a birthmark",
    "glasses": "glasses",
}

GENDER_NOUN = {"male": "man", "female": "woman"}


def _phrase(table, key, fallback_label=None):
    """Look up a phrase; otherwise treat the key as plain text and append fallback_label."""
    if not key:
        return ""
    text = str(key).strip()
    if text in table:
        return table[text]
    cleaned = text.replace("_", " ")
    if fallback_label:
        return f"{cleaned} {fallback_label}"
    return cleaned


def _color_phrase(value, kind):
    """skin / eye color → human readable. kind is 'skin' or 'eye'."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("#"):
        if kind == "skin":
            return f"a custom skin tone matching {text}"
        return f"eyes with a custom color matching {text}"
    if kind == "skin":
        return f"{text.replace('_', ' ')} skin tone"
    return f"{text.replace('_', ' ')} eyes"


def _natural_join(items):
    """Join with Oxford comma: ['a', 'b', 'c'] → 'a, b, and c'."""
    items = [item for item in items if item]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


class CharacterPromptCompiler:
    def compile(
        self,
        project_style=None,
        character=None,
        appearance=None,
        outfit=None,
        region="full_character",
        controls=None,
        text_refinement="",
        preserve=None,
        identity_locked=False,
        reference_images=None,
        image_type="portrait",
    ):
        controls = dict(controls or {})
        preserve = dict(preserve or {})
        reference_images = reference_images or []
        if identity_locked:
            preserve["identity"] = True

        description = self._describe_character(character, appearance, outfit, controls)
        style_value = controls.get("visual_style") or getattr(character, "visual_style", "") or project_style
        style_clause = f" in a {str(style_value).replace('_', ' ')} visual style" if style_value else ""

        if image_type == "portrait":
            positive_prompt = (
                f"A photorealistic single-character portrait of {description}{style_clause}. "
                "Head and shoulders, centered composition, looking at the camera, "
                "neutral expression, plain neutral studio background, soft studio lighting. "
                "The image contains only the character."
            )
        elif image_type == "full_body":
            positive_prompt = (
                f"A photorealistic single full-body image of {description}{style_clause}. "
                "Visible from head to toe, centered, neutral standing pose, "
                "plain neutral studio background, soft studio lighting. "
                "The image contains only the character."
            )
        elif image_type == "scene":
            positive_prompt = (
                f"A cinematic image of {description}{style_clause}, placed in an environment "
                "with mood lighting and cinematic framing."
            )
        elif image_type == "reference_sheet":
            positive_prompt = (
                f"A character reference sheet of {description}{style_clause}, showing front, "
                "side, and back views on a neutral background, consistent design across views."
            )
        else:
            positive_prompt = f"An image of {description}{style_clause}."

        if text_refinement:
            positive_prompt = f"{positive_prompt} {text_refinement}"

        if reference_images:
            positive_prompt = (
                f"{positive_prompt} Match the saved reference image so the character identity, "
                "face, hair, and clothing remain consistent across views."
            )

        outfit_desc = self._outfit_description(outfit)
        if outfit_desc and image_type != "scene":
            positive_prompt = (
                f"{positive_prompt} The character wears {outfit_desc} consistently across "
                "every view."
            )

        clothing_desc = (getattr(character, "clothing_description", "") or "").strip()
        if clothing_desc and image_type in ("portrait", "full_body", "reference_sheet"):
            positive_prompt = f"{positive_prompt} The character wears {clothing_desc}."

        logger.info(
            "compile: image_type=%s region=%s prompt_len=%d prompt=%r",
            image_type, region, len(positive_prompt), positive_prompt[:400],
        )

        edit_instruction = self._edit_instruction(region, controls, preserve, identity_locked, image_type)
        metadata = {
            "region": region,
            "image_type": image_type,
            "preserve": preserve,
            "identity_locked": identity_locked,
            "project_style": project_style,
            "controls": controls,
        }
        return {
            "positive_prompt": positive_prompt,
            "negative_prompt": NEGATIVES_BY_IMAGE_TYPE.get(image_type, NEGATIVE_BASE),
            "edit_instruction": edit_instruction,
            "reference_image_ids": reference_images,
            "metadata": metadata,
        }

    def compile_initial_portrait_selection(self, character, appearance, outfit, params):
        """
        Strict portrait-only prompt for the initial variant selection page.
        Builds the same natural-language description and adds tight portrait framing.
        """
        controls = dict(params or {})
        description = self._describe_character(character, appearance, outfit, controls)
        style_value = controls.get("visual_style") or getattr(character, "visual_style", "") or ""
        style_clause = f" in a {str(style_value).replace('_', ' ')} visual style" if style_value else ""

        positive_prompt = (
            f"A photorealistic head-and-shoulders portrait of {description}{style_clause}. "
            "Single character, centered, plain neutral studio background, soft studio lighting. "
            "The image contains only the character."
        )

        text_refinement = (controls.get("text_refinement") or controls.get("appearance_description") or "").strip()
        if text_refinement:
            positive_prompt = f"{positive_prompt} {text_refinement}"

        positive_prompt = (
            f"{positive_prompt} {INITIAL_PORTRAIT_FRAME} {PORTRAIT_VARIATION_GUIDE}"
        )

        logger.info(
            "compile_initial_portrait_selection: prompt_len=%d prompt=%r",
            len(positive_prompt), positive_prompt[:400],
        )

        return {
            "positive_prompt": positive_prompt,
            "negative_prompt": f"{INITIAL_PORTRAIT_NEGATIVE}, {CLEAN_FRAME_NEGATIVE}",
            "edit_instruction": "",
            "reference_image_ids": [],
            "metadata": {
                "region": "face",
                "image_type": "portrait",
                "mode": "initial_portrait_selection",
                "controls": controls,
            },
        }

    def _describe_character(self, character, appearance, outfit, controls):
        """Build a natural-language description of one character.
        Never emits 'key: value' or label-style fragments."""
        age = getattr(character, "age", None) if character else None
        gender = (getattr(character, "gender", "") if character else "") or ""
        char_type = (getattr(character, "character_type", "") if character else "") or "human"

        if char_type in ("animal", "creature", "robot", "object"):
            subject = f"a {char_type}"
        else:
            gender_noun = GENDER_NOUN.get(gender, "person")
            subject = f"a {age}-year-old {gender_noun}" if age else f"a {gender_noun}"

        short = (getattr(character, "short_description", "") or "").strip() if character else ""

        has_clauses = []
        if appearance:
            face_shape = controls.get("face_shape") or getattr(appearance, "face_shape", "")
            if face_shape:
                has_clauses.append(_phrase(FACE_SHAPE_PHRASE, face_shape, "face"))

            skin = controls.get("skin_tone") or getattr(appearance, "skin_tone", "")
            skin_phrase = _color_phrase(skin, "skin")
            if skin_phrase:
                has_clauses.append(skin_phrase)

            eye_color = controls.get("eye_color") or getattr(appearance, "eye_color", "")
            eye_color_phrase = _color_phrase(eye_color, "eye")
            if eye_color_phrase:
                has_clauses.append(eye_color_phrase)

            eye_shape = controls.get("eye_shape") or getattr(appearance, "eye_shape", "")
            if eye_shape:
                has_clauses.append(_phrase(EYE_SHAPE_PHRASE, eye_shape, "eyes"))

            eyebrow = controls.get("eyebrow_shape") or getattr(appearance, "eyebrow_shape", "")
            if eyebrow:
                has_clauses.append(_phrase(EYEBROW_SHAPE_PHRASE, eyebrow, "eyebrows"))

            nose = controls.get("nose_shape") or getattr(appearance, "nose_shape", "")
            if nose:
                has_clauses.append(_phrase(NOSE_SHAPE_PHRASE, nose, "nose"))

            lips = controls.get("lips_shape") or getattr(appearance, "lips_shape", "")
            if lips:
                has_clauses.append(_phrase(LIPS_SHAPE_PHRASE, lips, "lips"))

            hair_len = controls.get("hair_length") or getattr(appearance, "hair_length", "")
            hair_color = controls.get("hair_color") or getattr(appearance, "hair_color", "")
            hair_style = controls.get("hair_style") or getattr(appearance, "hair_style", "")
            hair_details_raw = controls.get("hair_details") or getattr(appearance, "hair_details", None) or []
            if isinstance(hair_details_raw, dict):
                hair_details_raw = [k for k, v in hair_details_raw.items() if v]
            hair_details = [str(d).replace("_", " ") for d in hair_details_raw if d]
            hair_clause = self._hair_clause(hair_len, hair_color, hair_style, hair_details)
            if hair_clause:
                has_clauses.append(hair_clause)

            body = controls.get("body_type") or getattr(appearance, "body_type", "")
            if body:
                has_clauses.append(_phrase(BODY_TYPE_PHRASE, body, "build"))

            distinctive = controls.get("distinctive_features") or getattr(appearance, "distinctive_features", []) or []
            if isinstance(distinctive, str):
                distinctive = [distinctive]
            for feature in distinctive:
                phrase = _phrase(DISTINCTIVE_FEATURE_PHRASE, feature)
                if phrase:
                    has_clauses.append(phrase)

        outfit_clause = ""
        outfit_desc = self._outfit_description(outfit)
        if outfit_desc:
            outfit_clause = f"wearing {outfit_desc}"

        pieces = [subject]
        if short:
            pieces.append(short)
        if has_clauses:
            pieces.append("with " + _natural_join(has_clauses))
        if outfit_clause:
            pieces.append(outfit_clause)

        return ", ".join(pieces)

    def _hair_clause(self, length, color, style, details=None):
        if not (length or color or style):
            return ""
        if length:
            base = _phrase(HAIR_LENGTH_PHRASE, length, "hair")
        else:
            base = "hair"
        extras = []
        if color:
            extras.append(str(color).replace("_", " "))
        if style and style != length and style != color:
            extras.append(str(style).replace("_", " "))
        if details:
            extras.extend(details)
        if extras:
            return f"{base} ({', '.join(extras)})"
        return base

    def _outfit_description(self, outfit):
        """Returns a canonical outfit description string, or empty string if no outfit."""
        if not outfit:
            return ""
        parts = []
        desc = (getattr(outfit, "description", "") or "").strip()
        style = (getattr(outfit, "style", "") or "").strip()
        if desc:
            parts.append(desc)
        if style:
            parts.append(f"{style} style")
        return ", ".join(parts)

    def _edit_instruction(self, region, controls, preserve, identity_locked, image_type):
        changed = ", ".join([f"{key}: {value}" for key, value in controls.items()]) or "requested controls"
        changed_fields = controls.get("changed_fields") or []
        if changed_fields:
            changed = ", ".join([str(field) for field in changed_fields])
        base = (
            "Preserve same character identity. Preserve unchanged attributes. "
            "Modify only changed fields. Keep same visual style unless style was changed. "
            f"Keep the same {image_type.replace('_', ' ')} composition."
        )
        if region == "hair":
            return (
                f"{base} Modify only hair ({changed}); keep face, age, eye color, body, outfit, pose, identity."
            )
        if region == "outfit":
            return f"{base} Modify only outfit ({changed}); keep face, hair, body, pose, age, identity."
        if region == "face" and identity_locked:
            return "Identity is locked; restrict face edits to expression-level changes only unless a new version is created."
        if region == "body":
            return f"{base} Modify only body silhouette or posture ({changed}); keep face, hair, outfit, age, identity."
        if region == "style":
            return f"{base} Modify only rendering style ({changed}); keep character identity, outfit structure, age, face, and body."
        return f"{base} Generate full character using structured controls ({changed}) while preserving requested fields."

    def _flatten(self, value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if isinstance(nested, (list, tuple)):
                    yield f"{key}: {', '.join([str(item) for item in nested])}"
                else:
                    yield f"{key}: {nested}"
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield str(item)
        else:
            yield str(value)
