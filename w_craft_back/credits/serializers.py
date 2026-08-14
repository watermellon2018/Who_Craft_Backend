from decimal import Decimal

from rest_framework import serializers


class DemoTopUpSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=Decimal("0.01"),
        max_value=Decimal("100000.00"),
    )


class CreditTransferSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, trim_whitespace=True)
    amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=Decimal("0.01"),
        max_value=Decimal("1000000.00"),
    )
    note = serializers.CharField(
        max_length=140,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        default="",
    )


class GenerationEstimateSerializer(serializers.Serializer):
    domain = serializers.ChoiceField(
        choices=("character", "poster", "reference", "music", "model3d")
    )
    operation = serializers.ChoiceField(
        choices=("generate", "edit", "reference"),
        default="generate",
    )
    modelKey = serializers.CharField(
        max_length=300,
        required=False,
        allow_blank=True,
        default="",
    )
    variantCount = serializers.IntegerField(min_value=1, max_value=100, default=1)
    promptLength = serializers.IntegerField(min_value=0, max_value=100000, default=0)
    resolution = serializers.ChoiceField(
        choices=("512", "1K", "2K", "4K"),
        default="1K",
    )
    routingMode = serializers.ChoiceField(
        choices=("manual", "economy", "fast", "balanced", "quality"),
        default="manual",
    )


class CreditAdminOperationSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, trim_whitespace=True)
    action = serializers.ChoiceField(
        choices=("adjustment", "refund", "freeze", "unfreeze")
    )
    amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=6,
        required=False,
        allow_null=True,
    )
    reason = serializers.CharField(max_length=255, trim_whitespace=True)
