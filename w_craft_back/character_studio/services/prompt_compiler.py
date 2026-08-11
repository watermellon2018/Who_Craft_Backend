import hashlib
import logging

from django.conf import settings

from w_craft_back.character_studio.constants import VISUAL_STYLES

logger = logging.getLogger(__name__)


def _log_compiled_prompt(
    *,
    prompt: str,
    image_type: str,
    region: str | None = None,
) -> None:
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    metadata = {
        "image_type": image_type,
        "prompt_hash": prompt_hash,
        "prompt_len": len(prompt),
    }
    if region:
        metadata["region"] = region
    logger.info("character_prompt_compiled", extra=metadata)

    # Break-glass diagnostics only: requires both this explicit flag and a
    # DEBUG logger level. Never enable in shared or production environments.
    if getattr(settings, "GENERATION_LOG_RAW_PROMPTS", False):
        logger.debug("character_prompt_raw_debug prompt=%r", prompt)


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

# Reserved for the upcoming 3D module — not consumed by the 2D prompt path.
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

        _log_compiled_prompt(
            prompt=positive_prompt,
            image_type=image_type,
            region=region,
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

        _log_compiled_prompt(
            prompt=positive_prompt,
            image_type="portrait",
            region="face",
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

    # framing recipes per image_type for identity-anchored generation.
    _IDENTITY_ANCHORED_FRAMING = {
        "full_body": (
            "full-body photo",
            (
                "Show from head to toe. Feet fully visible, head fully visible. "
                "Neutral standing pose. Centered composition. Character fully in frame."
            ),
        ),
        "three_quarter": (
            "3/4-view image",
            (
                "Head turned about 30 degrees away from camera. "
                "Upper-to-full body framing. Full silhouette readable."
            ),
        ),
        "profile": (
            "pure side profile view",
            (
                "Camera at exactly 90 degrees to the character. "
                "Clear silhouette of nose, chin, and forehead. "
                "Same hairstyle visible from the side."
            ),
        ),
        "back_view": (
            "back view image",
            (
                "Character facing fully away from the camera. Full body preferred. "
                "Hairstyle and outfit visible from behind. Feet fully visible. "
                "Neutral standing pose."
            ),
        ),
        "emotions": (
            "expression sheet",
            (
                "A grid of head-and-shoulders portraits showing several distinct "
                "facial expressions (neutral, happy, sad, angry, surprised). "
                "Same face identity in every cell. No text labels."
            ),
        ),
        "poses": (
            "pose sheet",
            (
                "Several full-body poses of the same character (idle, walking, "
                "action, sitting). Same outfit and hairstyle across poses. "
                "Animation/modeling reference style."
            ),
        ),
        "outfit_details": (
            "close-up outfit detail studies",
            (
                "Detailed close-ups of fabric, accessories, shoes, belts and "
                "jewelry of the outfit. Focus is wardrobe, not identity changes."
            ),
        ),
        "reference_sheet": (
            "character reference sheet",
            (
                "Front, side, and back views on a neutral background. "
                "Consistent design across views."
            ),
        ),
    }

    def compile_identity_anchored(self, character, appearance, outfit, image_type, params):
        """Prompt for image-to-image generation of a derived reference view.

        The reference image (uploaded multimodal alongside this prompt) is the
        authoritative source of the character's identity. The text only adds
        framing instructions and explicit identity-preservation constraints —
        we cannot rely on negative prompts for chat-completion models.
        """
        params = dict(params or {})
        controls = {**dict(params.get("controls") or {}), **params}
        preserve_identity = bool(params.get("preserve_identity", True))
        changed_fields = [
            str(field)
            for field in (
                params.get("changed_fields")
                or controls.get("changed_fields")
                or []
            )
        ]
        changed_field_set = set(changed_fields)
        previous_values = dict(
            params.get("previous_values")
            or controls.get("previous_values")
            or {}
        )
        new_values = dict(
            params.get("new_values")
            or controls.get("new_values")
            or {}
        )
        description = self._describe_character(
            character, appearance, outfit, controls,
        )
        framing_phrase, framing_instructions = self._IDENTITY_ANCHORED_FRAMING.get(
            image_type,
            (
                f"{image_type.replace('_', ' ')} image",
                "Keep the character recognizable. Centered composition. Plain neutral background.",
            ),
        )

        if preserve_identity:
            identity_constraints = [
                "- The output must depict the exact same individual as the reference.",
                "- Preserve face identity, facial structure, eyes, nose, lips, jawline, "
                "and facial proportions.",
            ]
            if "age" not in changed_field_set:
                identity_constraints.append("- Preserve the same age presentation.")
            if "gender" not in changed_field_set:
                identity_constraints.append("- Preserve the same gender presentation.")
            if not changed_field_set.intersection({"hair_length", "hair_color"}):
                identity_constraints.append(
                    "- Preserve the same hairstyle, hair color, and hair texture."
                )
            if "skin_tone" not in changed_field_set:
                identity_constraints.append(
                    "- Preserve the same skin tone and any distinctive marks or features."
                )
            if "visual_style" not in changed_field_set:
                identity_constraints.append(
                    "- Preserve the established visual style and rendering style."
                )
            identity_constraints.append(
                "- A different person is an invalid result. Reinterpreting the character "
                "is an invalid result."
            )
            identity_clause = (
                "STRICT IDENTITY CONSTRAINTS:\n"
                + "\n".join(identity_constraints)
                + "\n"
            )
        else:
            identity_clause = (
                "The reference image is the primary visual inspiration; minor stylistic "
                "variations are allowed but the character must remain recognizable.\n"
            )

        requested_changes = []
        for field in changed_fields:
            previous_value = previous_values.get(field)
            new_value = new_values.get(field, controls.get(field))
            if new_value is None:
                continue
            label = field.replace("_", " ")
            if previous_value is None:
                requested_changes.append(f"- Set {label} to {new_value}.")
            else:
                requested_changes.append(
                    f"- Change {label} from {previous_value} to {new_value}."
                )
        change_block = (
            "APPLY THESE REQUESTED CHARACTER CHANGES:\n"
            + "\n".join(requested_changes)
            + "\nPreserve identity while making these changes clearly visible.\n\n"
            if requested_changes
            else ""
        )

        positive_prompt = (
            f"Generate a {framing_phrase} of the SAME character shown in the "
            "provided reference image.\n\n"
            f"{identity_clause}\n"
            f"{change_block}FRAMING REQUIREMENTS:\n{framing_instructions}\n\n"
            "Do not change the outfit unless explicitly requested. "
            "Plain neutral studio background, soft studio lighting. "
            "The image must contain only this one character. "
            "No text, no logos, no watermarks.\n\n"
            "Character context (for clarification only — the reference image is "
            f"authoritative): {description}"
        )

        refinement = (
            (params.get("text_refinement") or params.get("correction_prompt") or "")
            .strip()
        )
        if refinement:
            positive_prompt = f"{positive_prompt}\n\nAdditional notes: {refinement}"

        negative = NEGATIVES_BY_IMAGE_TYPE.get(image_type, NEGATIVE_BASE)

        return {
            "positive_prompt": positive_prompt,
            "negative_prompt": negative,
            "edit_instruction": "",
            "reference_image_ids": [],
            "metadata": {
                "region": "full_character",
                "image_type": image_type,
                "mode": "identity_anchored",
                "preserve_identity": preserve_identity,
                "controls": controls,
            },
        }

    def compile_reference_prompt(self, character, appearance, outfit, params):
        """Prompt for image-to-image generation seeded by a user-uploaded reference.

        The user attached a real photo / artwork; the model should treat it as
        the source of identity. We pass it as the multimodal image part separately;
        this method only builds the accompanying text instruction.
        """
        controls = dict(params or {})
        preserve_identity = bool(controls.get("preserve_identity", True))
        description = self._describe_character(character, appearance, outfit, controls)
        style_value = (
            controls.get("visual_style")
            or getattr(character, "visual_style", "")
            or ""
        )
        style_clause = (
            f" in a {str(style_value).replace('_', ' ')} visual style"
            if style_value
            else ""
        )

        identity_clause = (
            "Preserve the face identity, key facial features, hair, body proportions, "
            "and distinctive traits from the provided reference image. "
            if preserve_identity
            else "Use the provided reference image as a soft visual inspiration. "
        )

        positive_prompt = (
            "Generate a clean, single-character WCraft Character Studio portrait based on "
            "the attached reference image. "
            f"{identity_clause}"
            f"The character is {description}{style_clause}. "
            "Head-and-shoulders or 3/4 character view, centered, plain neutral studio "
            "background, soft studio lighting, high detail, cinematic clean composition. "
            "Do not copy the original background, props, text, watermarks, or any extra "
            "people from the reference. The image must contain only the character."
        )

        text_refinement = (controls.get("text_refinement") or controls.get("refinement") or "").strip()
        if text_refinement:
            positive_prompt = f"{positive_prompt} Additional notes: {text_refinement}"

        negative = (
            f"{NEGATIVES_BY_IMAGE_TYPE['portrait']}, {CLEAN_FRAME_NEGATIVE}, "
            "background from reference, copied background, extra people from reference"
        )

        return {
            "positive_prompt": positive_prompt,
            "negative_prompt": negative,
            "edit_instruction": "",
            "reference_image_ids": [],
            "metadata": {
                "region": "full_character",
                "image_type": "portrait",
                "mode": "reference_seeded",
                "preserve_identity": preserve_identity,
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

            # body_type / height_cm intentionally omitted from the 2D prompt.
            # They remain on the appearance model for the upcoming 3D module,
            # where they will drive real parametric body shaping rather than
            # ambiguous prompt phrasing.

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
