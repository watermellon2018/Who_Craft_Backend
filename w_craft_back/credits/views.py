from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CreditAccount, CreditLedgerEntry, CreditOperationType
from .pricing import GenerationEstimate, estimate_for_model_key
from .serializers import (
    CreditTransferSerializer,
    DemoTopUpSerializer,
    GenerationEstimateSerializer,
)
from .services import (
    CreditServiceError,
    account_statistics,
    demo_top_up,
    get_or_create_account,
    list_entries,
    transfer_credits,
    validate_idempotency_key,
)
from w_craft_back.services.image_generation.errors import ImageProviderError
from w_craft_back.services.image_generation.registry import get_default_key


DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def _credit(value: Decimal) -> str:
    rendered = format(value, ".6f").rstrip("0").rstrip(".")
    whole, separator, fraction = rendered.partition(".")
    if not separator:
        return f"{whole}.00"
    return f"{whole}.{fraction.ljust(2, '0')}"


def _account_payload(account: CreditAccount) -> dict:
    return {
        "availableBalance": _credit(account.available_balance),
        "reservedBalance": _credit(account.reserved_balance),
        "totalBalance": _credit(account.total_balance),
    }


def _counterparty_payload(entry: CreditLedgerEntry) -> dict | None:
    user = entry.counterparty
    if user is None:
        return None
    profile = getattr(user, "profile", None)
    return {
        "username": user.username,
        "displayName": (
            profile.display_name if profile and profile.display_name else user.username
        ),
    }


def _entry_payload(entry: CreditLedgerEntry) -> dict:
    return {
        "id": str(entry.id),
        "operationType": entry.operation_type,
        "availableDelta": _credit(entry.available_delta),
        "reservedDelta": _credit(entry.reserved_delta),
        "availableBalanceAfter": _credit(entry.available_balance_after),
        "reservedBalanceAfter": _credit(entry.reserved_balance_after),
        "correlationId": str(entry.correlation_id),
        "counterparty": _counterparty_payload(entry),
        "description": entry.description,
        "createdAt": entry.created_at.isoformat(),
    }


def _error_response(error: CreditServiceError) -> Response:
    payload = {"code": error.code, "detail": error.message}
    if error.fields:
        payload["errors"] = error.fields
    return Response(payload, status=error.http_status)


def _validation_error(serializer) -> Response:
    return Response(
        {
            "code": "CREDIT_VALIDATION_ERROR",
            "detail": "Проверьте данные операции.",
            "errors": serializer.errors,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def _pagination(request) -> tuple[int, int] | Response:
    try:
        limit = int(request.query_params.get("limit", DEFAULT_LIMIT))
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        return Response(
            {
                "code": "INVALID_PAGINATION",
                "detail": "limit and offset must be integers.",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not 1 <= limit <= MAX_LIMIT or offset < 0:
        return Response(
            {
                "code": "INVALID_PAGINATION",
                "detail": (
                    f"limit must be 1-{MAX_LIMIT} and offset must be non-negative."
                ),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return limit, offset


class CreditSummaryView(APIView):
    def get(self, request):
        account = get_or_create_account(request.user)
        stats = account_statistics(account)
        return Response(
            {
                "account": _account_payload(account),
                "stats": {
                    "periodDays": stats["period_days"],
                    "received": _credit(stats["received"]),
                    "sent": _credit(stats["sent"]),
                    "spent": _credit(stats["spent"]),
                    "refunded": _credit(stats["refunded"]),
                },
                "capabilities": {
                    "demoTopUpEnabled": settings.CREDITS_DEMO_TOP_UP_ENABLED,
                    "transfersEnabled": True,
                },
            }
        )


class GenerationEstimateView(APIView):
    def post(self, request):
        serializer = GenerationEstimateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        data = serializer.validated_data
        try:
            if data["domain"] in {"music", "model3d"}:
                estimate = GenerationEstimate(
                    provider="local",
                    model_key="local",
                    model_name="local",
                    currency="USD",
                    estimated_cost=Decimal("0"),
                    reservation_amount=Decimal("0"),
                    pricing_source="local",
                    prompt_tokens_estimate=0,
                    snapshot={
                        "currency": "USD",
                        "source": "local",
                        "markup": "0",
                        "creditUsdRate": "1",
                    },
                )
            else:
                model_key = data["modelKey"] or (
                    getattr(getattr(request.user, "profile", None), "image_generation_model", "")
                    or get_default_key()
                )
                estimate = estimate_for_model_key(
                    model_key,
                    operation=data["operation"],
                    variant_count=data["variantCount"],
                    prompt_length=data["promptLength"],
                    resolution=data["resolution"],
                )
        except CreditServiceError as error:
            return _error_response(error)
        except ImageProviderError as error:
            return Response(
                {"code": error.code, "detail": error.message},
                status=error.http_status,
            )
        account = get_or_create_account(request.user)
        return Response(
            {
                "domain": data["domain"],
                "operation": data["operation"],
                "provider": estimate.provider,
                "modelKey": estimate.model_key,
                "modelName": estimate.model_name,
                "currency": estimate.currency,
                "estimatedCost": _credit(estimate.estimated_cost),
                "reservationAmount": _credit(estimate.reservation_amount),
                "pricingSource": estimate.pricing_source,
                "costIsEstimate": True,
                "availableBalance": _credit(account.available_balance),
                "sufficientBalance": (
                    account.available_balance >= estimate.reservation_amount
                ),
            }
        )


class CreditHistoryView(APIView):
    def get(self, request):
        pagination = _pagination(request)
        if isinstance(pagination, Response):
            return pagination
        limit, offset = pagination
        operation_type = request.query_params.get("operationType") or None
        if operation_type and operation_type not in CreditOperationType.values:
            return Response(
                {
                    "code": "INVALID_CREDIT_OPERATION_TYPE",
                    "detail": "Unknown credit operation type.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        account = get_or_create_account(request.user)
        filtered = account.ledger_entries.all()
        if operation_type:
            filtered = filtered.filter(operation_type=operation_type)
        total = filtered.count()
        entries = list_entries(
            account,
            limit=limit,
            offset=offset,
            operation_type=operation_type,
        )
        next_offset = offset + limit if offset + limit < total else None
        return Response(
            {
                "items": [_entry_payload(entry) for entry in entries],
                "total": total,
                "limit": limit,
                "offset": offset,
                "nextOffset": next_offset,
            }
        )


class CreditDemoTopUpView(APIView):
    def post(self, request):
        if not settings.CREDITS_DEMO_TOP_UP_ENABLED:
            return Response(
                {
                    "code": "DEMO_TOP_UP_DISABLED",
                    "detail": "Демонстрационное пополнение отключено.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = DemoTopUpSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            key = validate_idempotency_key(request.headers.get("Idempotency-Key"))
            result = demo_top_up(
                user=request.user,
                amount=serializer.validated_data["amount"],
                idempotency_key=key,
            )
        except CreditServiceError as error:
            return _error_response(error)

        response_status = (
            status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
        )
        return Response(
            {
                "account": {
                    "availableBalance": _credit(result.entry.available_balance_after),
                    "reservedBalance": _credit(result.entry.reserved_balance_after),
                    "totalBalance": _credit(
                        result.entry.available_balance_after
                        + result.entry.reserved_balance_after
                    ),
                },
                "transaction": _entry_payload(result.entry),
                "replayed": result.replayed,
            },
            status=response_status,
        )


class CreditTransferView(APIView):
    def post(self, request):
        serializer = CreditTransferSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            key = validate_idempotency_key(request.headers.get("Idempotency-Key"))
            result = transfer_credits(
                sender=request.user,
                recipient_username=serializer.validated_data["username"],
                amount=serializer.validated_data["amount"],
                note=serializer.validated_data["note"],
                idempotency_key=key,
            )
        except CreditServiceError as error:
            return _error_response(error)

        response_status = (
            status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
        )
        return Response(
            {
                "account": {
                    "availableBalance": _credit(
                        result.sender_entry.available_balance_after
                    ),
                    "reservedBalance": _credit(
                        result.sender_entry.reserved_balance_after
                    ),
                    "totalBalance": _credit(
                        result.sender_entry.available_balance_after
                        + result.sender_entry.reserved_balance_after
                    ),
                },
                "transfer": {
                    "id": str(result.sender_entry.correlation_id),
                    "amount": _credit(-result.sender_entry.available_delta),
                    "recipient": _counterparty_payload(result.sender_entry),
                    "note": result.sender_entry.description,
                    "createdAt": result.sender_entry.created_at.isoformat(),
                },
                "replayed": result.replayed,
            },
            status=response_status,
        )
