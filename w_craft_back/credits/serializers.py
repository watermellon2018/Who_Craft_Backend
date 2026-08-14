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
