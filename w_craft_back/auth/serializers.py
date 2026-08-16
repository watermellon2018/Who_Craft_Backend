from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True)
    username = serializers.CharField(required=True, min_length=3, max_length=150)

    def validate(self, attrs):
        user = User(username=attrs["username"])
        try:
            validate_password(attrs["password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": exc.messages}) from exc
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(
            password=validated_data['password'],
            username=validated_data['username'],
            last_login=None,
        )

    class Meta:
        model = User
        fields = ['username', 'password']
