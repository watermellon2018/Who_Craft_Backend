import logging

from w_craft_back.character_studio.constants import VISUAL_STYLES

logger = logging.getLogger(__name__)

CHARACTER_TYPE_LABELS = {
    "human": "human",
    "animal": "animal",
    "creature": "creature",
    "robot": "robot",
    "object": "object",
    "other": "other",
}


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

# Enforces identical outfit across portrait / full_body / scene / reference_sheet.
# Placed after the image-type composition instruction so it has high weight.
OUTFIT_LOCK = (
    "OUTFIT CONSISTENCY REQUIRED: the character must wear exactly the same outfit in "
    "every image type (portrait, full body, scene, reference sheet). "
    "Outfit: {outfit_desc}. "
    "DO NOT change, replace, or reinterpret clothing between views."
)

IMAGE_TYPE_PROMPTS = {
    "portrait": (
        "Portrait composition: face and shoulders, readable facial expression, stable identity, "
        "clear age cues, skin, hair, eyes and facial structure in focus."
    ),
    "full_body": (
        "Full body composition: whole character visible head to toe, body proportions, clothing, "
        "silhouette and pose are clearly readable."
    ),
    "scene": (
        "Scene composition: the character is placed in an environment with background, mood, "
        "lighting and cinematic framing."
    ),
    "reference_sheet": (
        "Reference sheet composition: front, side and back views on a neutral background, "
        "consistent character design, useful for future video generation."
    ),
}


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

        profile_bits = self._profile_bits(character, appearance, outfit, controls)
        style = controls.get("visual_style") or getattr(character, "visual_style", "") or project_style
        if style in VISUAL_STYLES or style:
            profile_bits.append(f"{style.replace('_', ' ')} style")

        positive_prompt = "Create a clean character design of " + ", ".join(
            [bit for bit in profile_bits if bit]
        )
        if text_refinement:
            positive_prompt = f"{positive_prompt}. Contextual refinement: {text_refinement}"
        if reference_images:
            positive_prompt = (
                f"{positive_prompt}. Use the saved reference image to preserve the exact same "
                "character identity across face, proportions, hair, clothing logic and visual style."
            )
        if image_type in IMAGE_TYPE_PROMPTS:
            positive_prompt = f"{positive_prompt}. {IMAGE_TYPE_PROMPTS[image_type]}"

        outfit_desc = self._outfit_description(outfit)
        if outfit_desc:
            positive_prompt = f"{positive_prompt}. {OUTFIT_LOCK.format(outfit_desc=outfit_desc)}"

        logger.debug(
            "compile: image_type=%s region=%s outfit=%s prompt_len=%d",
            image_type, region, outfit_desc or "none", len(positive_prompt),
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
            "negative_prompt": NEGATIVE_BASE,
            "edit_instruction": edit_instruction,
            "reference_image_ids": reference_images,
            "metadata": metadata,
        }

    def compile_initial_portrait_selection(self, character, appearance, outfit, params):
        """
        Strict portrait-only prompt for the initial variant selection page.
        Enforces: portrait frame, all user-specified attributes fixed, variation only in
        angle/expression/lighting.
        """
        controls = dict(params or {})
        profile_bits = self._profile_bits(character, appearance, outfit, controls)

        style = controls.get("visual_style") or getattr(character, "visual_style", "") or ""
        if style:
            profile_bits.append(f"{style.replace('_', ' ')} style")

        positive_prompt = "Create a clean character portrait of " + ", ".join(
            [bit for bit in profile_bits if bit]
        )

        text_refinement = (controls.get("text_refinement") or controls.get("appearance_description") or "").strip()
        if text_refinement:
            positive_prompt = f"{positive_prompt}. {text_refinement}"

        locked = self._locked_attrs_instruction(character, appearance, outfit, controls)
        if locked:
            positive_prompt = f"{positive_prompt}. STRICTLY PRESERVE IN EVERY VARIANT: {locked}"

        positive_prompt = (
            f"{positive_prompt}. "
            f"{INITIAL_PORTRAIT_FRAME} "
            f"{PORTRAIT_VARIATION_GUIDE}"
        )

        logger.debug(
            "compile_initial_portrait_selection: outfit=%s locked_attrs=%s prompt_len=%d",
            self._outfit_description(outfit) or "none", locked or "none", len(positive_prompt),
        )

        return {
            "positive_prompt": positive_prompt,
            "negative_prompt": INITIAL_PORTRAIT_NEGATIVE,
            "edit_instruction": "",
            "reference_image_ids": [],
            "metadata": {
                "region": "face",
                "image_type": "portrait",
                "mode": "initial_portrait_selection",
                "controls": controls,
            },
        }

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

    def _locked_attrs_instruction(self, character, appearance, outfit, controls):
        """Returns a comma-separated string of all user-specified fixed attributes."""
        attrs = []

        if character:
            char_type = getattr(character, "character_type", "") or ""
            if char_type:
                attrs.append(f"{CHARACTER_TYPE_LABELS.get(char_type, char_type)} entity type")
            gender = getattr(character, "gender", "") or ""
            if gender and gender != "other":
                attrs.append(f"{gender} gender")
            age = getattr(character, "age", None)
            if age:
                attrs.append(f"{age} years old")

        appearance_fields = [
            ("hair_color", "hair color"),
            ("hair_length", "hair length"),
            ("hair_style", "hairstyle"),
            ("eye_color", "eye color"),
            ("skin_tone", "skin tone"),
            ("face_shape", "face shape"),
        ]
        for field, label in appearance_fields:
            value = (controls.get(field) or "").strip()
            if not value and appearance:
                value = (getattr(appearance, field, "") or "").strip()
            if value:
                attrs.append(f"{value} {label}")

        special = (controls.get("special_features") or
                   (getattr(appearance, "special_features", "") if appearance else "") or "").strip()
        if special:
            attrs.append(f"special features: {special}")

        outfit_desc = self._outfit_description(outfit)
        if outfit_desc:
            attrs.append(f"outfit: {outfit_desc}")

        return ", ".join(attrs) if attrs else ""

    def _profile_bits(self, character, appearance, outfit, controls):
        bits = []
        if character:
            age = getattr(character, "age", None)
            lifecycle_stage = getattr(character, "lifecycle_stage", "")
            gender = getattr(character, "gender", "")
            character_type = getattr(character, "character_type", "human")
            species = getattr(character, "species", "human")
            if character_type:
                bits.append(f"entity type: {CHARACTER_TYPE_LABELS.get(character_type, character_type)}")
            if lifecycle_stage:
                bits.append(f"life stage: {lifecycle_stage}")
            if age:
                bits.append(f"a {age}-year-old")
            if gender:
                bits.append(f"gender applicability: {gender}")
            if species:
                bits.append(species)
            if getattr(character, "short_description", ""):
                bits.append(character.short_description)
            personality = getattr(character, "personality", {}) or {}
            if personality:
                bits.append("personality: " + ", ".join(self._flatten(personality)))
        if appearance:
            for field in (
                "skin_tone",
                "eye_color",
                "eye_shape",
                "face_shape",
                "hair_length",
                "hair_style",
                "hair_color",
                "body_type",
                "height",
                "posture",
            ):
                value = controls.get(field) or getattr(appearance, field, "")
                if value:
                    bits.append(f"{value.replace('_', ' ')} {field.replace('_', ' ')}")
            body_structure = controls.get("body_structure") or getattr(appearance, "body_structure", "")
            surface_material = controls.get("surface_material") or getattr(appearance, "surface_material", "")
            special_features = controls.get("special_features") or getattr(appearance, "special_features", "")
            appearance_prompt = controls.get("appearance_description") or getattr(appearance, "appearance_prompt", "")
            if body_structure:
                bits.append(f"body structure: {body_structure}")
            if surface_material:
                bits.append(f"surface material: {surface_material}")
            if special_features:
                bits.append(f"special features: {special_features}")
            if appearance_prompt:
                bits.append(f"appearance: {appearance_prompt}")
            distinctive = controls.get("distinctive_features") or getattr(appearance, "distinctive_features", [])
            if distinctive:
                bits.append("distinctive features: " + ", ".join(distinctive))
        if outfit:
            if getattr(outfit, "description", ""):
                bits.append(f"wearing {outfit.description}")
            if getattr(outfit, "style", ""):
                bits.append(f"{outfit.style} outfit")
        if controls.get("layers"):
            bits.append("outfit layers: " + str(controls["layers"]))
        return bits

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
