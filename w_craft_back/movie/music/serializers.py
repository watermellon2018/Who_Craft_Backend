"""Camel-case public validation for Music Studio requests."""

from __future__ import annotations

from collections.abc import Mapping

from django.conf import settings
from rest_framework import serializers


CONTENT_MODES = ("instrumental", "song")
VARIANT_COUNTS = (1, 2)
PURPOSES = ("underscore", "ambience", "transition", "stinger", "song")
GENRES = (
    "cinematic",
    "cinematic_pop",
    "ambient",
    "electronic",
    "orchestral",
    "acoustic",
    "experimental",
    "pop",
)
MOODS = (
    "tense",
    "mysterious",
    "hopeful",
    "melancholic",
    "warm",
    "dark",
    "triumphant",
    "romantic",
)
INSTRUMENTS = (
    "low_strings",
    "full_strings",
    "piano",
    "acoustic_guitar",
    "electric_guitar",
    "analog_pulse",
    "synth_pad",
    "drums",
    "percussion",
    "brass",
    "woodwinds",
    "choir",
)
ENERGY_CURVES = ("steady", "build", "peak", "fade")
TEMPO_MODES = ("auto", "slow", "medium", "fast", "bpm")
LYRICS_LANGUAGES = ("ru", "en")
LYRICS_SECTION_TYPES = ("verse", "chorus", "bridge", "outro")
VOCAL_TIMBRES = ("warm", "clear", "dark", "airy", "bright")
VOCAL_DELIVERIES = ("intimate", "soft", "powerful", "spoken", "rap")
VOCAL_DENSITIES = ("sparse", "balanced", "dense")
REFERENCE_RIGHTS_STATEMENT_VERSION = "music-reference-v1"
MAX_TEXT_REFINEMENT_LENGTH = 1000
MAX_LYRICS_SECTIONS = 30
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_MUSIC_SEED = 4_294_967_295


def music_min_duration_seconds() -> int:
    """Return the effective minimum requested output duration."""

    return max(1, int(getattr(settings, "MUSIC_MIN_DURATION_SECONDS", 3)))


def music_max_duration_seconds() -> int:
    """Return the effective maximum requested output duration."""

    configured = int(getattr(settings, "MUSIC_MAX_DURATION_SECONDS", 300))
    return max(music_min_duration_seconds(), configured)


def music_max_lyrics_chars() -> int:
    """Return the effective aggregate author-lyrics limit."""

    return max(1, int(getattr(settings, "MUSIC_MAX_LYRICS_CHARS", 12_000)))


def _validate_unique(values: list, field_name: str) -> list:
    if len(values) != len(set(values)):
        raise serializers.ValidationError(f"{field_name} values must be unique.")
    return values


def _default_project_context() -> dict[str, str]:
    return {"type": "project"}


class MusicContextSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=("project", "scene"))
    sceneId = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs: dict) -> dict:
        if attrs["type"] == "scene" and attrs.get("sceneId") is None:
            raise serializers.ValidationError(
                {"sceneId": "This field is required for scene context."}
            )
        if attrs["type"] == "project" and "sceneId" in attrs:
            raise serializers.ValidationError(
                {"sceneId": "Project context cannot include a scene."}
            )
        return attrs


class VocalStyleSerializer(serializers.Serializer):
    timbre = serializers.ChoiceField(choices=VOCAL_TIMBRES, required=False)
    delivery = serializers.ChoiceField(choices=VOCAL_DELIVERIES, required=False)
    density = serializers.ChoiceField(choices=VOCAL_DENSITIES, required=False)


class LyricsSectionSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=LYRICS_SECTION_TYPES)
    label = serializers.CharField(max_length=100, allow_blank=True, required=False)
    text = serializers.CharField(
        allow_blank=False,
        trim_whitespace=False,
        max_length=music_max_lyrics_chars,
    )


class MusicContentSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=CONTENT_MODES)
    lyricsLanguage = serializers.ChoiceField(
        choices=LYRICS_LANGUAGES,
        required=False,
    )
    vocalStyle = VocalStyleSerializer(required=False)
    sections = LyricsSectionSerializer(
        many=True,
        required=False,
        max_length=MAX_LYRICS_SECTIONS,
    )

    def validate(self, attrs: dict) -> dict:
        mode = attrs["mode"]
        sections = attrs.get("sections", [])
        language = attrs.get("lyricsLanguage")
        vocal_style = attrs.get("vocalStyle")
        if mode == "instrumental":
            disallowed = {}
            if sections:
                disallowed["sections"] = "Instrumental briefs cannot include lyrics."
            if language:
                disallowed["lyricsLanguage"] = (
                    "Instrumental briefs cannot include a lyrics language."
                )
            if vocal_style:
                disallowed["vocalStyle"] = (
                    "Instrumental briefs cannot include a vocal style."
                )
            if disallowed:
                raise serializers.ValidationError(disallowed)
            return {"mode": mode}

        errors = {}
        if not language:
            errors["lyricsLanguage"] = "This field is required for songs."
        if not sections:
            errors["sections"] = "At least one lyrics section is required."
        total_chars = sum(len(section.get("text", "")) for section in sections)
        if total_chars > music_max_lyrics_chars():
            errors["sections"] = (
                f"Lyrics must contain at most {music_max_lyrics_chars()} characters."
            )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class TempoSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=TEMPO_MODES)
    bpm = serializers.IntegerField(min_value=40, max_value=220, required=False)

    def validate(self, attrs: dict) -> dict:
        if attrs["mode"] == "bpm" and attrs.get("bpm") is None:
            raise serializers.ValidationError(
                {"bpm": "This field is required for exact BPM mode."}
            )
        if attrs["mode"] != "bpm" and "bpm" in attrs:
            raise serializers.ValidationError(
                {"bpm": "BPM is only accepted in exact BPM mode."}
            )
        return attrs


class MusicBriefSerializer(serializers.Serializer):
    context = MusicContextSerializer(
        required=False,
        default=_default_project_context,
    )
    content = MusicContentSerializer()
    title = serializers.CharField(max_length=255, trim_whitespace=True)
    purpose = serializers.ChoiceField(choices=PURPOSES)
    genre = serializers.ChoiceField(choices=GENRES)
    moods = serializers.ListField(
        child=serializers.ChoiceField(choices=MOODS),
        min_length=1,
        max_length=3,
    )
    durationSeconds = serializers.IntegerField()
    tempo = TempoSerializer()
    energyCurve = serializers.ChoiceField(choices=ENERGY_CURVES)
    instruments = serializers.ListField(
        child=serializers.ChoiceField(choices=INSTRUMENTS),
        max_length=6,
        required=False,
        default=list,
    )
    exclude = serializers.ListField(
        child=serializers.CharField(max_length=50),
        max_length=6,
        required=False,
        default=list,
    )
    loopable = serializers.BooleanField(required=False, default=False)
    seed = serializers.IntegerField(
        min_value=0,
        max_value=MAX_MUSIC_SEED,
        allow_null=True,
        required=False,
    )
    textRefinement = serializers.CharField(
        max_length=MAX_TEXT_REFINEMENT_LENGTH,
        allow_blank=True,
        trim_whitespace=False,
        required=False,
        default="",
    )

    def validate_title(self, value: str) -> str:
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Title cannot be empty.")
        return title

    def validate_moods(self, values: list[str]) -> list[str]:
        return _validate_unique(values, "Mood")

    def validate_instruments(self, values: list[str]) -> list[str]:
        return _validate_unique(values, "Instrument")

    def validate_exclude(self, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            item = value.strip()
            if not item:
                raise serializers.ValidationError("Excluded values cannot be empty.")
            normalized.append(item)
        return _validate_unique(normalized, "Excluded")

    def validate_durationSeconds(self, value: int) -> int:
        minimum = music_min_duration_seconds()
        maximum = music_max_duration_seconds()
        if not minimum <= value <= maximum:
            raise serializers.ValidationError(
                f"Duration must be between {minimum} and {maximum} seconds."
            )
        return value

    def validate(self, attrs: dict) -> dict:
        mode = attrs["content"]["mode"]
        purpose = attrs["purpose"]
        if mode == "song" and purpose != "song":
            raise serializers.ValidationError(
                {"purpose": "Song content requires the song purpose."}
            )
        if mode == "instrumental" and purpose == "song":
            raise serializers.ValidationError(
                {"purpose": "The song purpose requires song content."}
            )
        return attrs


class GenerationCreateSerializer(serializers.Serializer):
    modelKey = serializers.CharField(
        max_length=64,
        required=False,
        allow_blank=False,
    )
    targetTrackId = serializers.IntegerField(
        min_value=1,
        allow_null=True,
        required=False,
    )
    referenceAssetId = serializers.UUIDField(allow_null=True, required=False)
    variantCount = serializers.ChoiceField(choices=VARIANT_COUNTS, default=2)
    brief = MusicBriefSerializer()


class ReferenceUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    rightsConfirmed = serializers.BooleanField()
    rightsStatementVersion = serializers.CharField(max_length=64)

    def validate_rightsConfirmed(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError("Usage rights must be confirmed.")
        return value

    def validate_rightsStatementVersion(self, value: str) -> str:
        if value != REFERENCE_RIGHTS_STATEMENT_VERSION:
            raise serializers.ValidationError("Unsupported rights statement version.")
        return value


class TrackPatchSerializer(serializers.Serializer):
    expectedTrackVersion = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=255, required=False)
    author = serializers.CharField(max_length=255, allow_blank=True, required=False)
    durationSeconds = serializers.IntegerField(min_value=0, required=False)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        max_length=30,
        required=False,
    )
    activeVersionId = serializers.UUIDField(allow_null=False, required=False)

    def to_internal_value(self, data: Mapping[str, object]) -> dict:
        if not isinstance(data, Mapping):
            return super().to_internal_value(data)
        unknown_fields = sorted(set(data.keys()) - set(self.fields))
        if unknown_fields:
            raise serializers.ValidationError(
                {
                    field: ["Unknown field."]
                    for field in unknown_fields
                }
            )
        return super().to_internal_value(data)

    def validate_title(self, value: str) -> str:
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Title cannot be empty.")
        return title

    def validate_tags(self, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise serializers.ValidationError("Tags cannot be empty.")
        return _validate_unique(normalized, "Tag")


class ArchiveTrackSerializer(serializers.Serializer):
    expectedTrackVersion = serializers.IntegerField(min_value=1)


class AssignmentItemSerializer(serializers.Serializer):
    sceneId = serializers.IntegerField(min_value=1)
    trackVersionId = serializers.UUIDField()
    startTimeSeconds = serializers.IntegerField(min_value=0, default=0)


class AssignmentReplaceSerializer(serializers.Serializer):
    expectedTrackVersion = serializers.IntegerField(min_value=1)
    items = AssignmentItemSerializer(many=True, allow_empty=True)

    def validate_items(self, values: list[dict]) -> list[dict]:
        scene_ids = [item["sceneId"] for item in values]
        _validate_unique(scene_ids, "Scene")
        return values


class ApplyVariantSerializer(serializers.Serializer):
    targetTrackId = serializers.IntegerField(min_value=1, allow_null=True, required=False)
    expectedTrackVersion = serializers.IntegerField(
        min_value=1,
        allow_null=True,
        required=False,
    )
    title = serializers.CharField(max_length=255)
    author = serializers.CharField(max_length=255, allow_blank=True, required=False)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        max_length=30,
        required=False,
        default=list,
    )
    makeActive = serializers.BooleanField(required=False, default=True)

    def validate_title(self, value: str) -> str:
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Title cannot be empty.")
        return title

    def validate(self, attrs: dict) -> dict:
        if attrs.get("targetTrackId") is not None and attrs.get(
            "expectedTrackVersion"
        ) is None:
            raise serializers.ValidationError(
                {"expectedTrackVersion": "This field is required for an existing track."}
            )
        return attrs
