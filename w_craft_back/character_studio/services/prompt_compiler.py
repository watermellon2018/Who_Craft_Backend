from w_craft_back.character_studio.constants import VISUAL_STYLES

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
