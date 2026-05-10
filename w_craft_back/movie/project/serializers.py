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
    ProjectGenerationJob,
    ProjectTag,
    Scene,
)
from w_craft_back.movie.project.models import Project, ProjectStatus


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

    # Editor fields (legacy columns).
    format = serializers.ChoiceField(
        choices=PROJECT_FORMAT_CHOICES, required=False, default="feature_film"
    )
    genre = _genre_field()
    audience = _audience_field()
    annotation = serializers.CharField(
        max_length=2000, allow_blank=True, required=False, default=""
    )
    synopsis = serializers.CharField(
        max_length=5000, allow_blank=True, required=False, default=""
    )
    # Base64 data URL for poster upload (kept compatible with the old endpoint).
    poster_image_data = serializers.CharField(
        allow_blank=True, required=False, default=""
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
        max_length=2000, allow_blank=True, required=False
    )
    synopsis = serializers.CharField(
        max_length=5000, allow_blank=True, required=False
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


class SceneCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(allow_blank=True, required=False, default="")
    script_text = serializers.CharField(allow_blank=True, required=False, default="")
    location_id = serializers.IntegerField(required=False, allow_null=True)
    order = serializers.IntegerField(required=False, min_value=0)


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


class GenerationJobCreateSerializer(serializers.Serializer):
    job_type = serializers.CharField(max_length=30)
    prompt = serializers.CharField(allow_blank=True, required=False, default="")
    negative_prompt = serializers.CharField(allow_blank=True, required=False, default="")
    input_data = serializers.JSONField(required=False, default=dict)
