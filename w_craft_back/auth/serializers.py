import uuid
from rest_framework import serializers
from django.contrib.auth.models import User

from w_craft_back.auth.models import UserKey


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    def create(self, validated_data):
        user = User.objects.create_user(
            password=validated_data.get('password', None),
            username=validated_data.get('username', ''),
        )

        return user

    class Meta:
        model = User
        fields = ['username', 'password']


class UserKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = UserKey
        fields = ['user', 'key']
