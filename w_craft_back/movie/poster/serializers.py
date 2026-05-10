"""DRF serializers for the project poster generation API."""

from __future__ import annotations

from rest_framework import serializers

from w_craft_back.movie.poster.models import PosterFormat, PosterStyle


PROMPT_MAX_LENGTH = 1000


class PosterGenerateSerializer(serializers.Serializer):
    prompt = serializers.CharField(
        max_length=PROMPT_MAX_LENGTH,
        allow_blank=False,
        trim_whitespace=True,
    )
    style = serializers.ChoiceField(choices=PosterStyle.choices)
    format = serializers.ChoiceField(choices=PosterFormat.choices)
    reference_image_url = serializers.CharField(
        allow_blank=True,
        allow_null=True,
        required=False,
        default="",
    )
    reference_image_asset_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )

    def validate_prompt(self, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("Prompt is required")
        return cleaned


class PosterSelectSerializer(serializers.Serializer):
    variant_id = serializers.IntegerField(min_value=1)
