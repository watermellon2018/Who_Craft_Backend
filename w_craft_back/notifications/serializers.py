from rest_framework import serializers

from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            'id',
            'type',
            'title',
            'message',
            'created_at',
            'is_read',
            'target_url',
            'entity_type',
            'entity_id',
        ]


class NotificationReadSerializer(serializers.Serializer):
    is_read = serializers.BooleanField()

    def validate_is_read(self, value):
        if value is not True:
            raise serializers.ValidationError('only marking as read is supported')
        return value
