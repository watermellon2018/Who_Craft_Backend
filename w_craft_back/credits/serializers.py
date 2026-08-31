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
    senderUsername = serializers.CharField(max_length=150, trim_whitespace=True)
    recipientUsername = serializers.CharField(max_length=150, trim_whitespace=True)
    amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=Decimal("0.01"),
        max_value=Decimal("1000000.00"),
    )
    reason = serializers.CharField(
        max_length=255,
        trim_whitespace=True,
    )


class GenerationEstimateSerializer(serializers.Serializer):
    domain = serializers.ChoiceField(
        choices=(
            "character",
            "poster",
            "reference",
            "storyboard",
            "music",
            "sound_effect",
            "model3d",
        )
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
    durationSeconds = serializers.FloatField(
        min_value=0.5,
        max_value=600,
        required=False,
        allow_null=True,
        default=None,
    )
    resolution = serializers.ChoiceField(
        choices=("512", "1K", "2K", "4K"),
        default="1K",
    )
    routingMode = serializers.ChoiceField(
        choices=("manual", "economy", "fast", "balanced", "quality"),
        default="manual",
    )


class CreditAdminOperationSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("freeze", "unfreeze"))
    reason = serializers.CharField(max_length=255, trim_whitespace=True)


class ProjectCreditBudgetSerializer(serializers.Serializer):
    limit = serializers.DecimalField(
        max_digits=18,
        decimal_places=6,
        allow_null=True,
        min_value=Decimal("0"),
    )
