"""Validation for the snake-case messaging API contract."""

from typing import Any

from rest_framework import serializers


MAX_MESSAGE_LENGTH = 4000


class PageSerializer(serializers.Serializer):
    limit = serializers.IntegerField(default=30, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)


class SearchSerializer(PageSerializer):
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    q = serializers.CharField(default='', allow_blank=True, max_length=100)


class HistorySerializer(serializers.Serializer):
    before = serializers.IntegerField(required=False, min_value=1)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=100)


class ChangesSerializer(serializers.Serializer):
    after_revision = serializers.IntegerField(default=0, min_value=0)
    limit = serializers.IntegerField(default=100, min_value=1, max_value=100)


class OpenConversationSerializer(serializers.Serializer):
    recipient_id = serializers.IntegerField(min_value=1)


class MessageTextField(serializers.CharField):
    def to_internal_value(self, data: Any) -> str:
        """JSON numbers must not silently become message text."""
        if not isinstance(data, str):
            self.fail('invalid')
        return super().to_internal_value(data)


class MessageTextSerializer(serializers.Serializer):
    text = MessageTextField(max_length=MAX_MESSAGE_LENGTH)

    def validate_text(self, value: str) -> str:
        """Reject malformed Unicode before hashing or sending it to PostgreSQL."""
        try:
            value.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise serializers.ValidationError('Invalid Unicode text.') from exc
        return value


class SendMessageSerializer(MessageTextSerializer):
    client_id = serializers.UUIDField()


class EditMessageSerializer(MessageTextSerializer):
    expected_version = serializers.IntegerField(min_value=1)


class ReadSerializer(serializers.Serializer):
    through_sequence = serializers.IntegerField(min_value=0)
