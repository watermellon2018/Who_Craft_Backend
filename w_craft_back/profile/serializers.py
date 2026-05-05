from rest_framework import serializers
from .models import UserProfile


class UserProfileSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = ['language', 'private_account', 'notifications_enabled']
