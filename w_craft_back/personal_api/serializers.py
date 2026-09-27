"""Strict bounded request schemas; keys are write-only input."""

from rest_framework import serializers

from .models import PROVIDERS, SOURCES


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data: object) -> dict:
        if isinstance(data, dict) and set(data) - set(self.fields):
            raise serializers.ValidationError({
                'non_field_errors': ['Unsupported request fields.'],
            })
        return super().to_internal_value(data)


class SecretField(serializers.CharField):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            min_length=8, max_length=4096, trim_whitespace=True,
            write_only=True, **kwargs,
        )

    def to_internal_value(self, data: object) -> str:
        secret = super().to_internal_value(data)
        if any(char.isspace() or ord(char) < 32 for char in secret):
            raise serializers.ValidationError('Enter a key without whitespace.')
        return secret


class CreateConnectionSerializer(StrictSerializer):
    name = serializers.CharField(max_length=100)
    provider = serializers.ChoiceField(choices=PROVIDERS)
    apiKey = SecretField()


class UpdateConnectionSerializer(StrictSerializer):
    name = serializers.CharField(max_length=100, required=False)
    apiKey = SecretField(required=False)
    isActive = serializers.BooleanField(required=False)

    def validate(self, attrs: dict) -> dict:
        if not attrs:
            raise serializers.ValidationError('Provide a field to update.')
        return attrs


class RouteSerializer(StrictSerializer):
    connectionId = serializers.UUIDField()
    modelKey = serializers.CharField(max_length=300)


class PreferenceSerializer(StrictSerializer):
    source = serializers.ChoiceField(choices=SOURCES)
    routes = RouteSerializer(many=True, max_length=8)
