class CharacterProfileParser:
    def parse(self, description):
        text = (description or "").lower()
        appearance = {
            "source_type": "description",
            "source_description": description or "",
            "appearance_prompt": description or "",
        }
        personality = {}

        if "green" in text:
            appearance["eye_color"] = "green"
        elif "blue" in text:
            appearance["eye_color"] = "blue"
        elif "brown" in text:
            appearance["eye_color"] = "brown"

        if "red hair" in text or "copper" in text:
            appearance["hair_color"] = "copper"
        elif "black hair" in text:
            appearance["hair_color"] = "black"
        elif "blonde" in text:
            appearance["hair_color"] = "blonde"

        if "slim" in text:
            appearance["body_type"] = "slim"
        elif "athletic" in text:
            appearance["body_type"] = "athletic"

        if "anxious" in text:
            personality["temperament"] = "anxious"
        if "sarcastic" in text:
            personality["traits"] = ["sarcastic"]

        return {"appearance": appearance, "personality": personality}

