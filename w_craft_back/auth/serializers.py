import uuid
from rest_framework import serializers
from django.contrib.auth.models import User

from w_craft_back.auth.models import UserKey


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, min_length=8)
    username = serializers.CharField(required=True, min_length=3, max_length=150)

    def create(self, validated_data):
        return User.objects.create_user(
            password=validated_data['password'],
            username=validated_data['username'],
            last_login=None,
        )

    class Meta:
        model = User
        fields = ['username', 'password']


class UserKeySerializer(serializers.ModelSerializer):
    class Meta:
        model = UserKey
        # `user` FK omitted: exposing the linked auth.User PK has no client use
        # and gives attackers a stable identifier to correlate against.
        fields = ['key']
