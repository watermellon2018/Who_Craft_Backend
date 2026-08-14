"""Input serializers for the project Reference Library."""

from __future__ import annotations

from rest_framework import serializers

from w_craft_back.movie.reference_library.models import (
    ReferenceCategory,
    ReferenceCharacterRelation,
    ReferenceOperation,
    SceneReferenceUsage,
)
from w_craft_back.movie.reference_library.prompt_compiler import normalize_brief


class CharacterLinkSerializer(serializers.Serializer):
    characterId = serializers.UUIDField()
    relation = serializers.ChoiceField(choices=ReferenceCharacterRelation.values)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ReferenceCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255, trim_whitespace=True)
    category = serializers.ChoiceField(choices=ReferenceCategory.values)
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=4000,
    )
    brief = serializers.JSONField(required=False, default=dict)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50, trim_whitespace=True),
        required=False,
        default=list,
        max_length=20,
    )
    locationId = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    characterLinks = CharacterLinkSerializer(many=True, required=False, default=list)

    def validate_brief(self, value):
        return normalize_brief(value)


class ReferencePatchSerializer(serializers.Serializer):
    version = serializers.IntegerField(min_value=1)
    title = serializers.CharField(required=False, max_length=255, trim_whitespace=True)
    category = serializers.ChoiceField(required=False, choices=ReferenceCategory.values)
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=4000,
    )
    brief = serializers.JSONField(required=False)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50, trim_whitespace=True),
        required=False,
        max_length=20,
    )
    locationId = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    characterLinks = CharacterLinkSerializer(many=True, required=False)

    def validate_brief(self, value):
        return normalize_brief(value)


class ExpectedReferenceVersionSerializer(serializers.Serializer):
    expectedReferenceVersion = serializers.IntegerField(min_value=1)


class ReferenceUploadSerializer(ExpectedReferenceVersionSerializer):
    file = serializers.FileField()
    rightsConfirmed = serializers.BooleanField()
    rightsStatementVersion = serializers.CharField(max_length=64)

    def validate_rightsConfirmed(self, value):
        if value is not True:
            raise serializers.ValidationError("must be true")
        return value


class GenerationCreateSerializer(ExpectedReferenceVersionSerializer):
    operation = serializers.ChoiceField(choices=ReferenceOperation.values)
    sourceVersionId = serializers.UUIDField(required=False, allow_null=True)
    variantCount = serializers.IntegerField()
    imageModel = serializers.CharField(required=False, allow_blank=True, max_length=128)
    routingMode = serializers.ChoiceField(
        choices=("manual", "economy", "fast", "balanced", "quality"),
        required=False,
        default="manual",
    )
    brief = serializers.JSONField(required=False)
    editInstruction = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=2000,
    )

    def validate_brief(self, value):
        return normalize_brief(value)

    def validate(self, attrs):
        operation = attrs["operation"]
        count = attrs["variantCount"]
        if operation == ReferenceOperation.GENERATE and count not in (1, 2, 4):
            raise serializers.ValidationError({"variantCount": ["must be 1, 2 or 4"]})
        if operation == ReferenceOperation.EDIT:
            if count != 1:
                raise serializers.ValidationError(
                    {"variantCount": ["must equal 1 for edit"]}
                )
            if not attrs.get("sourceVersionId"):
                raise serializers.ValidationError(
                    {"sourceVersionId": ["this field is required"]}
                )
            if not str(attrs.get("editInstruction", "")).strip():
                raise serializers.ValidationError(
                    {"editInstruction": ["this field is required"]}
                )
        return attrs


class SceneReferenceItemSerializer(serializers.Serializer):
    referenceId = serializers.UUIDField()
    versionId = serializers.UUIDField()
    usage = serializers.ChoiceField(choices=SceneReferenceUsage.values)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)


class SceneReferenceReplaceSerializer(serializers.Serializer):
    expectedSceneVersion = serializers.IntegerField(min_value=1)
    items = SceneReferenceItemSerializer(many=True, max_length=100)

    def validate_items(self, value):
        ids = [item["referenceId"] for item in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("referenceId values must be unique")
        return value
