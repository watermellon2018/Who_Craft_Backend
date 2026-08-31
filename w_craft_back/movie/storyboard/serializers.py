"""Input serializers for the Storyboard domain."""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from w_craft_back.movie.storyboard.models import (
    CameraAzimuth,
    CameraDistance,
    CameraElevation,
    CameraFraming,
    CameraMovement,
    GenerationReferenceType,
    ShotReferenceRole,
)


class ShotVisualReferenceSerializer(serializers.Serializer):
    referenceId = serializers.UUIDField()
    role = serializers.ChoiceField(choices=ShotReferenceRole.values)


class ShotRelationValidationMixin:
    def validate_characterIds(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("ids must be unique")
        return value

    def validate_visualReferences(self, value):
        keys = [(item["referenceId"], item["role"]) for item in value]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError("references must be unique")
        return value


class ShotCreateSerializer(ShotRelationValidationMixin, serializers.Serializer):
    title = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=255,
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=8000,
    )
    durationSeconds = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=8,
        decimal_places=2,
        min_value=Decimal("0"),
    )
    locationId = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )
    characterIds = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        default=list,
        max_length=100,
    )
    visualReferences = ShotVisualReferenceSerializer(
        many=True,
        required=False,
        default=list,
    )


class ShotPatchSerializer(ShotRelationValidationMixin, serializers.Serializer):
    expectedVersion = serializers.IntegerField(min_value=1)
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=8000,
    )
    durationSeconds = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=8,
        decimal_places=2,
        min_value=Decimal("0"),
    )
    locationId = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )
    characterIds = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        max_length=100,
    )
    visualReferences = ShotVisualReferenceSerializer(many=True, required=False)


class ShotReorderSerializer(serializers.Serializer):
    shotIds = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=True,
        max_length=1000,
    )

    def validate_shotIds(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("shotIds must be unique")
        return value


class KeyframeCreateSerializer(serializers.Serializer):
    position = serializers.DecimalField(
        max_digits=5,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        max_value=Decimal("0.9999"),
    )


class KeyframePatchSerializer(KeyframeCreateSerializer):
    pass


class CameraIntentSerializer(serializers.Serializer):
    expectedVersion = serializers.IntegerField(required=False, min_value=1)
    target = serializers.JSONField()
    azimuth = serializers.ChoiceField(choices=CameraAzimuth.values)
    elevation = serializers.ChoiceField(choices=CameraElevation.values)
    distance = serializers.ChoiceField(choices=CameraDistance.values)
    framing = serializers.ChoiceField(choices=CameraFraming.values)
    lensMm = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=10,
        max_value=300,
    )
    composition = serializers.JSONField(required=False, default=list)
    cameraMetadata = serializers.JSONField(required=False, default=dict)


class TransitionPatchSerializer(serializers.Serializer):
    movementOverride = serializers.ChoiceField(
        choices=CameraMovement.values,
        allow_null=True,
    )


class GenerationReferenceSerializer(serializers.Serializer):
    referenceType = serializers.ChoiceField(choices=GenerationReferenceType.values)
    sourceKeyframeId = serializers.UUIDField(required=False, allow_null=True)
    visualReferenceId = serializers.UUIDField(required=False, allow_null=True)
    characterId = serializers.UUIDField(required=False, allow_null=True)
    locationId = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    priority = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=32767,
        default=0,
    )
    isPrimary = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        target_fields = (
            "sourceKeyframeId",
            "visualReferenceId",
            "characterId",
            "locationId",
        )
        supplied = [field for field in target_fields if attrs.get(field) is not None]
        if len(supplied) != 1:
            raise serializers.ValidationError(
                "exactly one reference target must be supplied"
            )
        return attrs


class GenerationReferencesReplaceSerializer(serializers.Serializer):
    references = GenerationReferenceSerializer(many=True, max_length=100)

    def validate_references(self, value):
        if sum(bool(item["isPrimary"]) for item in value) > 1:
            raise serializers.ValidationError("only one primary reference is allowed")
        return value


class GenerateKeyframeSerializer(serializers.Serializer):
    imageModel = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
    )
    routingMode = serializers.ChoiceField(
        choices=("manual", "economy", "fast", "balanced", "quality"),
        required=False,
        default="manual",
    )


class ShotListSuggestSerializer(serializers.Serializer):
    language = serializers.ChoiceField(choices=("ru", "en"), required=False)
    model = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=200,
    )
    maxShots = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=40,
        default=16,
    )
