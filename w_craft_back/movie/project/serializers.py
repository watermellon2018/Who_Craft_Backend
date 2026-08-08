"""Project / dashboard serializers.

Read-side dashboard data is assembled in ``services.py`` as plain dicts
(camelCase) for compactness and explicit shape control. These serializers
cover write paths (create/update) for the dashboard's CRUD action endpoints.
"""

from __future__ import annotations

from rest_framework import serializers

from w_craft_back.character_studio.models import CharacterRole, StudioCharacter
from w_craft_back.movie.project.dashboard_models import (
    Location,
    MusicTrack,
    ProjectTag,
    Scene,
    SceneStatus,
    SceneType,
)
from w_craft_back.movie.project.models import Project, ProjectStatus

PROJECT_ANNOTATION_MAX_LENGTH = 800
PROJECT_SYNOPSIS_MAX_LENGTH = 2000

# --------------------------------------------------------------------------- #
# Project create / update
# --------------------------------------------------------------------------- #

# Format choices accepted on write paths. Stored as plain strings on Project.format
# (the legacy column is a CharField), but validated here so the editor cannot push
# arbitrary values.
PROJECT_FORMAT_CHOICES = (
    ("short_film", "Короткометражный фильм"),
    ("feature_film", "Полнометражный фильм"),
    ("series", "Сериал"),
    ("clip", "Клип"),
    ("commercial", "Реклама"),
    ("other", "Другое"),
    # Legacy values still present in some rows / older frontend builds.
    ("full-movie", "Полнометражный фильм (legacy)"),
    ("short-movie", "Короткометражка (legacy)"),
    ("marketing", "Реклама (legacy)"),
)

PROJECT_TARGET_AUDIENCE_CHOICES = (
    ("all", "Все"),
    ("kids", "Дети"),
    ("teens", "Подростки"),
    ("young_adults", "Молодёжь"),
    ("adults", "Взрослые"),
    ("elderly", "Пожилые люди"),
)


def _genre_field():
    return serializers.ListField(
        child=serializers.CharField(max_length=120, allow_blank=False),
        required=False,
        allow_empty=True,
    )


def _audience_field():
    return serializers.ListField(
        child=serializers.CharField(max_length=255, allow_blank=False),
        required=False,
        allow_empty=True,
    )


class ProjectGenerationSettingsSerializer(serializers.Serializer):
    image_generation_model = serializers.CharField(
        max_length=100,
        allow_blank=True,
        required=False,
    )

    def validate_image_generation_model(self, value):
        from w_craft_back.services.image_generation import MODEL_REGISTRY

        normalized = (value or "").strip()
        legacy = {"mock", "gemini", "google", "imagen"}
        if (
            normalized
            and normalized not in MODEL_REGISTRY
            and normalized.lower() not in legacy
        ):
            raise serializers.ValidationError("Unknown image generation model.")
        return normalized


class ProjectCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(allow_blank=True, required=False, default="")
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False,
        allow_empty=True,
        default=list,
    )
    status = serializers.ChoiceField(
        choices=ProjectStatus.choices,
        required=False,
        default=ProjectStatus.DRAFT,
    )
    is_favorite = serializers.BooleanField(required=False, default=False)
    generation_settings = ProjectGenerationSettingsSerializer(
        required=False,
        default=dict,
    )

    # Editor fields (legacy columns).
    format = serializers.ChoiceField(
        choices=PROJECT_FORMAT_CHOICES, required=False, default="feature_film"
    )
    genre = _genre_field()
    audience = _audience_field()
    annotation = serializers.CharField(
        max_length=PROJECT_ANNOTATION_MAX_LENGTH,
        allow_blank=True,
        required=False,
        default="",
    )
    synopsis = serializers.CharField(
        max_length=PROJECT_SYNOPSIS_MAX_LENGTH,
        allow_blank=True,
        required=False,
        default="",
    )
    # Base64 data URL for poster upload (kept compatible with the old endpoint).
    poster_image_data = serializers.CharField(
        allow_blank=True,
        required=False,
        default=""
    )

    def validate_title(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Title is required")
        return value


class ProjectUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255, required=False)
    description = serializers.CharField(allow_blank=True, required=False)
    status = serializers.ChoiceField(choices=ProjectStatus.choices, required=False)
    is_favorite = serializers.BooleanField(required=False)
    generation_settings = ProjectGenerationSettingsSerializer(required=False)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False,
        allow_empty=True,
    )

    format = serializers.ChoiceField(
        choices=PROJECT_FORMAT_CHOICES, required=False
    )
    genre = _genre_field()
    audience = _audience_field()
    annotation = serializers.CharField(
        max_length=PROJECT_ANNOTATION_MAX_LENGTH,
        allow_blank=True,
        required=False,
    )
    synopsis = serializers.CharField(
        max_length=PROJECT_SYNOPSIS_MAX_LENGTH,
        allow_blank=True,
        required=False,
    )
    poster_image_data = serializers.CharField(allow_blank=True, required=False)
    # Pass an empty string explicitly to clear the poster.
    poster_url = serializers.CharField(
        allow_blank=True, allow_null=True, required=False
    )

    def validate_title(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Title cannot be empty")
        return value


# --------------------------------------------------------------------------- #
# Domain object serializers (write-side validation)
# --------------------------------------------------------------------------- #

class CharacterCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    short_description = serializers.CharField(allow_blank=True, required=False, default="")
    role = serializers.ChoiceField(
        choices=CharacterRole.choices,
        required=False,
        default=CharacterRole.SECONDARY,
    )


SCRIPT_BLOCK_TYPES = (
    "scene_heading",
    "action",
    "character",
    "dialogue",
    "remark",
    "camera",
    "transition",
    "sound",
    "note",
)


class ScriptBlockSerializer(serializers.Serializer):
    id = serializers.CharField(max_length=100, allow_blank=False)
    type = serializers.ChoiceField(choices=SCRIPT_BLOCK_TYPES)
    text = serializers.CharField(allow_blank=True, trim_whitespace=False)
    characterId = serializers.UUIDField(required=False, allow_null=True)


class _SceneWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255, required=False)
    description = serializers.CharField(allow_blank=True, required=False)
    script_text = serializers.CharField(
        allow_blank=True,
        required=False,
        trim_whitespace=False
    )
    script_blocks = ScriptBlockSerializer(many=True, required=False)
    location_id = serializers.IntegerField(required=False, allow_null=True)
    status = serializers.ChoiceField(choices=SceneStatus.choices, required=False)
    order = serializers.IntegerField(required=False, min_value=0)
    act = serializers.IntegerField(required=False, min_value=1, max_value=3)
    duration_seconds = serializers.IntegerField(required=False, min_value=0)
    mood = serializers.CharField(max_length=100, allow_blank=True, required=False)
    scene_type = serializers.ChoiceField(choices=SceneType.choices, required=False)
    notes = serializers.CharField(
        allow_blank=True,
        required=False,
        trim_whitespace=False
    )
    camera_settings = serializers.JSONField(required=False)
    character_ids = serializers.ListField(
        child=serializers.UUIDField(), allow_empty=True, required=False
    )

    def validate_title(self, value: str) -> str:
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Title cannot be empty")
        return title

    def validate_character_ids(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Character ids must be unique")
        self._validate_project_characters(value)
        return value

    def validate_script_blocks(self, value):
        character_ids = {
            block["characterId"]
            for block in value
            if block.get("characterId") is not None
        }
        self._validate_project_characters(character_ids)
        return [
            {
                **block,
                **(
                    {"characterId": str(block["characterId"])}
                    if block.get("characterId") is not None
                    else {}
                ),
            }
            for block in value
        ]

    def _validate_project_characters(self, character_ids) -> None:
        project = self.context.get("project")
        if project is None or not character_ids:
            return
        found_ids = set(
            StudioCharacter.objects.filter(
                project=project, character_id__in=character_ids
            ).values_list("character_id", flat=True)
        )
        if set(character_ids) - found_ids:
            raise serializers.ValidationError("Character not found in this project")


class SceneCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(allow_blank=True, required=False, default="")
    script_text = serializers.CharField(allow_blank=True, required=False, default="")
    location_id = serializers.IntegerField(required=False, allow_null=True)
    order = serializers.IntegerField(required=False, min_value=0)


class SceneWorkspaceCreateSerializer(_SceneWriteSerializer):
    title = serializers.CharField(max_length=255)


class SceneWorkspaceUpdateSerializer(_SceneWriteSerializer):
    version = serializers.IntegerField(min_value=1, required=True)


class MusicTrackCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    author = serializers.CharField(max_length=255, allow_blank=True, required=False, default="")
    duration_seconds = serializers.IntegerField(required=False, min_value=0, default=0)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50),
        required=False,
        allow_empty=True,
        default=list,
    )


class LocationCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    description = serializers.CharField(allow_blank=True, required=False, default="")
