import re

from w_craft_back.character_studio.services.errors import SafetyRejectedError


class CharacterSafetyService:
    NSFW_PATTERNS = (
        r"\bnsfw\b",
        r"\bnude\b",
        r"\bnaked\b",
        r"\bporn\b",
        r"\berotic\b",
        r"\bsex\b",
        r"\bsexual\b",
        r"\bfetish\b",
    )

    MINOR_PATTERNS = (
        r"\bminor\b",
        r"\bteen\b",
        r"\bunderage\b",
        r"\bchild\b",
        r"\bschoolgirl\b",
        r"\bschoolboy\b",
    )

    def validate_user_text(self, *texts):
        joined = " ".join([text for text in texts if text]).lower()
        if not joined:
            return True
        if any(re.search(pattern, joined) for pattern in self.NSFW_PATTERNS):
            if any(re.search(pattern, joined) for pattern in self.MINOR_PATTERNS):
                raise SafetyRejectedError("Sexualized minor context is not allowed.")
            raise SafetyRejectedError()
        return True

    def validate_generated_prompt(self, prompt):
        return self.validate_user_text(prompt)

    def validate_uploaded_image_placeholder(self, metadata=None):
        return True

