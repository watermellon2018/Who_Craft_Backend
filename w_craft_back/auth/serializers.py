from rest_framework import serializers
from django.contrib.auth.models import User


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data.get('email', None),
            password=validated_data.get('password', None),
            username=validated_data.get('username', ''),
        )

        return user

    class Meta:
        model = User
        fields = ['email', 'username', 'password']
