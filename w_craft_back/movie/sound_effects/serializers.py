"""HTTP input validation for the Sound Effects domain."""

from decimal import Decimal

from rest_framework import serializers


MODEL_KEY = "elevenlabs-sound-effects-v2"


class GenerationCreateSerializer(serializers.Serializer):
    modelKey = serializers.ChoiceField(choices=(MODEL_KEY,), default=MODEL_KEY)
    prompt = serializers.CharField(max_length=450, trim_whitespace=True)
    durationSeconds = serializers.FloatField(
        min_value=0.5,
        max_value=30,
        allow_null=True,
        required=False,
        default=None,
    )
    loop = serializers.BooleanField(default=False)
    promptInfluence = serializers.FloatField(
        min_value=Decimal("0"),
        max_value=1,
        default=0.3,
    )
    targetEffectId = serializers.IntegerField(
        min_value=1,
        allow_null=True,
        required=False,
    )
    sceneId = serializers.IntegerField(
        min_value=1,
        allow_null=True,
        required=False,
    )

    def validate_prompt(self, value: str) -> str:
        prompt = value.strip()
        if not prompt:
            raise serializers.ValidationError("Prompt cannot be empty.")
        return prompt


class ApplyVariantSerializer(serializers.Serializer):
    targetEffectId = serializers.IntegerField(
        min_value=1,
        allow_null=True,
        required=False,
    )
    title = serializers.CharField(max_length=255, trim_whitespace=True)

    def validate_title(self, value: str) -> str:
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Title cannot be empty.")
        return title


class AssignmentSerializer(serializers.Serializer):
    sceneId = serializers.IntegerField(min_value=1)
    effectVersionId = serializers.UUIDField()
    startTimeSeconds = serializers.DecimalField(
        max_digits=10,
        decimal_places=3,
        min_value=Decimal("0"),
    )
