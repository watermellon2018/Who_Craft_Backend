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

FULL_BODY_FRAMING_NEGATIVE = (
    "cropped feet, cropped head, partial body, half-body shot, close-up, "
    "feet out of frame, head out of frame, low-angle crop, knees-up shot"
)

NEGATIVES_BY_IMAGE_TYPE = {
    "portrait": f"{NEGATIVE_BASE}, {CLEAN_FRAME_NEGATIVE}",
    "full_body": f"{NEGATIVE_BASE}, {CLEAN_FRAME_NEGATIVE}, {FULL_BODY_FRAMING_NEGATIVE}",
    "scene": NEGATIVE_BASE,
    "reference_sheet": NEGATIVE_BASE,
    "three_quarter": f"{NEGATIVE_BASE}, {CLEAN_FRAME_NEGATIVE}",
    "profile": f"{NEGATIVE_BASE}, {CLEAN_FRAME_NEGATIVE}",
    "back_view": f"{NEGATIVE_BASE}, {CLEAN_FRAME_NEGATIVE}, {FULL_BODY_FRAMING_NEGATIVE}",
    "emotions": NEGATIVE_BASE,
    "poses": NEGATIVE_BASE,
    "outfit_details": NEGATIVE_BASE,
}

# Reference views must NEVER drift in identity, hair, or outfit. Appended to
# every reference-stage prompt regardless of image_type — this is the lock the
# References screen relies on.
REFERENCE_IDENTITY_TAIL = (
    "Do not change face identity. Do not change hairstyle unless requested. "
    "Do not change outfit unless requested. Maintain consistent character design. "
    "Plain neutral background. Clear, even lighting. No extra characters. "
    "No text, no watermarks, no logos."
)

# All image_types that belong to the References stage. Used by compile() to
# decide whether to append REFERENCE_IDENTITY_TAIL.
REFERENCE_IMAGE_TYPES = (
    "portrait",
    "full_body",
    "three_quarter",
    "profile",
    "back_view",
    "emotions",
    "poses",
    "outfit_details",
    "reference_sheet",
)

# Extra negatives appended when doing a zone / local edit.
ZONE_EDIT_NEGATIVE = (
    "partial removal, half-removed object, ghost of removed object, "
    "artifact, residue, leftover trace, blurry region, seam, "
    "inconsistent lighting at boundary, smeared area"
)


# --- Enum → natural-language phrase tables ---------------------------------
# Each phrase is a noun phrase that fits inside a sentence WITHOUT a "label:"
# prefix. This is what stops the model from rendering character cards.

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
        zone_edit=None,
        correction_prompt="",
        preserve_identity=True,
    ):
        controls = dict(controls or {})
        preserve = dict(preserve or {})
        reference_images = reference_images or []
        if identity_locked:
            preserve["identity"] = True
        if preserve_identity:
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
                "Full body, standing on flat ground, feet fully visible, head fully visible, "
                "character fully in frame, no cropping of feet or head, neutral standing pose, "
                "centered, plain neutral studio background, soft studio lighting. "
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
        elif image_type == "three_quarter":
            positive_prompt = (
                f"A 3/4 view single-character image of {description}{style_clause}. "
                "Head turned about 30 degrees away from camera, full silhouette readable, "
                "upper-to-full body framing, plain neutral studio background, soft studio lighting. "
                "The image contains only the character."
            )
        elif image_type == "profile":
            positive_prompt = (
                f"A pure side profile view of {description}{style_clause}. "
                "Camera at exactly 90 degrees to the character. Clear, readable silhouette of "
                "nose, chin, and forehead. Same hairstyle and outfit as the canonical reference. "
                "Plain neutral studio background, soft studio lighting. "
                "The image contains only the character."
            )
        elif image_type == "back_view":
            positive_prompt = (
                f"A back view of {description}{style_clause}. "
                "Character facing fully away from the camera, full body preferred, hairstyle "
                "and outfit visible from behind, neutral standing pose, feet fully visible, "
                "plain neutral studio background, soft studio lighting. "
                "The image contains only the character."
            )
        elif image_type == "emotions":
            positive_prompt = (
                f"An expression sheet of {description}{style_clause}. "
                "A grid of head-and-shoulders portraits showing several distinct facial "
                "expressions (neutral, happy, sad, angry, surprised). Same face identity "
                "in every cell, same hairstyle, same outfit. Plain neutral background, "
                "even diffuse lighting. No text labels."
            )
        elif image_type == "poses":
            positive_prompt = (
                f"A pose sheet of {description}{style_clause}. "
                "Several full-body poses of the same character (idle, walking, action, "
                "sitting). Same outfit, same hairstyle, consistent proportions across poses. "
                "Plain neutral background, even diffuse lighting. Animation/modeling reference style."
            )
        elif image_type == "outfit_details":
            positive_prompt = (
                f"Close-up outfit detail studies of {description}{style_clause}. "
                "Detailed close-ups of fabric, accessories, shoes, belts and jewelry of the "
                "outfit. No identity changes; the focus is the wardrobe. Plain neutral "
                "background, even diffuse lighting."
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
        if clothing_desc and image_type in (
            "portrait", "full_body", "reference_sheet",
            "three_quarter", "profile", "back_view",
        ):
            positive_prompt = f"{positive_prompt} The character wears {clothing_desc}."

        # Identity-lock tail for the References stage. Prevents face/hair/outfit
        # drift across the multiple reference views the user has to approve.
        if image_type in REFERENCE_IMAGE_TYPES:
            positive_prompt = f"{positive_prompt} {REFERENCE_IDENTITY_TAIL}"

        # User-supplied correction text (e.g., "the side profile face changed,
        # restore identity"). preserve_identity=True forbids fundamental
        # identity drift; the correction is scoped to this single reference.
        correction = (correction_prompt or "").strip()
        if correction:
            scope = image_type.replace("_", " ")
            if preserve_identity:
                positive_prompt = (
                    f"{positive_prompt} USER CORRECTION (apply ONLY to this {scope} reference): "
                    f"{correction}. Do NOT change the character identity, face, hairstyle, or "
                    "outfit beyond this correction."
                )
            else:
                positive_prompt = (
                    f"{positive_prompt} USER CORRECTION (apply ONLY to this {scope} reference): "
                    f"{correction}."
                )

        zone_edit_meta = None
        if zone_edit and isinstance(zone_edit, dict) and zone_edit.get("selection"):
            sel = zone_edit["selection"]
            instr = (zone_edit.get("instruction") or "").strip()
            quadrant = self._describe_quadrant(sel)
            positive_prompt = self._build_zone_edit_prompt(
                positive_prompt, instr, sel, quadrant
            )
            zone_edit_meta = {"selection": sel, "instruction": instr, "quadrant": quadrant}

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
        if zone_edit_meta:
            metadata["zone_edit"] = zone_edit_meta
        if correction:
            metadata["correction_prompt"] = correction
            metadata["preserve_identity"] = bool(preserve_identity)
        base_negative = NEGATIVES_BY_IMAGE_TYPE.get(image_type, NEGATIVE_BASE)
        negative_prompt = (
            f"{base_negative}, {ZONE_EDIT_NEGATIVE}" if zone_edit_meta else base_negative
        )
        return {
            "positive_prompt": positive_prompt,
            "negative_prompt": negative_prompt,
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

        # appearance_prompt is the primary face/feature description when set.
        appearance_prompt = (
            controls.get("appearance_description")
            or (getattr(appearance, "appearance_prompt", "") if appearance else "")
            or ""
        )

        has_clauses = []
        if appearance:
            skin = controls.get("skin_tone") or getattr(appearance, "skin_tone", "")
            skin_phrase = _color_phrase(skin, "skin")
            if skin_phrase:
                has_clauses.append(skin_phrase)

            # Simplified hair: only length and color (style/details deprecated).
            hair_len = controls.get("hair_length") or getattr(appearance, "hair_length", "")
            hair_color = controls.get("hair_color") or getattr(appearance, "hair_color", "")
            hair_clause = self._hair_clause(hair_len, hair_color, None, [])
            if hair_clause:
                has_clauses.append(hair_clause)

            body = controls.get("body_type") or getattr(appearance, "body_type", "")
            if body:
                has_clauses.append(_phrase(BODY_TYPE_PHRASE, body, "build"))

            height_cm = controls.get("height_cm")
            if height_cm in (None, "", 0):
                height_cm = getattr(appearance, "height_cm", None)
            if height_cm:
                try:
                    height_cm_int = int(height_cm)
                    if 50 <= height_cm_int <= 280:
                        has_clauses.append(f"approximately {height_cm_int} cm tall")
                except (TypeError, ValueError):
                    pass

        outfit_clause = ""
        outfit_desc = self._outfit_description(outfit)
        if outfit_desc:
            outfit_clause = f"wearing {outfit_desc}"

        pieces = [subject]
        # Primary face/feature description wins over individual field clauses.
        if appearance_prompt:
            pieces.append(appearance_prompt)
        elif short:
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

    def _build_zone_edit_prompt(self, base_prompt, instruction, sel, quadrant):
        """Construct a strong local-edit instruction block appended to the base prompt.

        The wording is deliberately imperative and explicit to counteract model
        tendencies to ignore localised change requests. Key principles:
        - "THIS IS A LOCAL EDIT" framing comes first so it isn't buried.
        - The user instruction is repeated verbatim in multiple forms.
        - Hard prohibitions on partial compliance and artifacts.
        - Region coordinates give the model a spatial anchor even without a mask.
        """
        x = float(sel["x"])
        y = float(sel["y"])
        w = float(sel["width"])
        h = float(sel["height"])
        instr_upper = instruction.upper()
        return (
            f"{base_prompt} "
            f"THIS IS A LOCAL EDIT — apply ONLY to the {quadrant} region "
            f"(normalized x={x:.2f}–{x+w:.2f}, y={y:.2f}–{y+h:.2f}). "
            f"USER INSTRUCTION: {instruction}. "
            f"MANDATORY: {instr_upper}. "
            "Execute the instruction completely and literally. "
            "If the instruction says to remove an object, remove it entirely — "
            "no partial removal, no artifacts, no trace left. "
            "If the instruction says to add or change something, do it fully within the region. "
            "DO NOT ignore or soften this instruction. "
            "DO NOT keep the original content if removal is requested. "
            "ALL areas OUTSIDE this region must remain pixel-identical — "
            "do NOT alter the face, hair, clothing, background, or any other part of the image "
            "outside the specified rectangle. "
            "Preserve the character's overall identity, style, and proportions."
        )

    def _describe_quadrant(self, selection):
        cx = float(selection["x"]) + float(selection["width"]) / 2.0
        cy = float(selection["y"]) + float(selection["height"]) / 2.0
        if cy < 1.0 / 3.0:
            row = "upper"
        elif cy < 2.0 / 3.0:
            row = "middle"
        else:
            row = "lower"
        if cx < 1.0 / 3.0:
            col = "left"
        elif cx < 2.0 / 3.0:
            col = "center"
        else:
            col = "right"
        if row == "middle" and col == "center":
            return "center"
        if row == "middle":
            return col
        if col == "center":
            return row
        return f"{row}-{col}"

    def _edit_instruction(self, region, controls, preserve, identity_locked, image_type):
        if controls.get("zone_edit"):
            zone_instr = (controls.get("zone_instruction") or "").strip()
            base = (
                "THIS IS A LOCAL ZONE EDIT. "
                "Apply the user instruction ONLY within the specified rectangular region. "
                "Execute the instruction completely — if removal is requested, remove the object entirely. "
                "Do NOT leave partial artifacts or traces of the removed object. "
                "Do NOT modify any area outside the selected rectangle. "
                "Preserve the character's identity, face, style, and all unaffected regions. "
                f"Keep the same {image_type.replace('_', ' ')} composition."
            )
            if zone_instr:
                return f"{base} User instruction: {zone_instr}."
            return base
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
