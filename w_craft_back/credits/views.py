from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from w_craft_back.movie.music.providers import (
    MusicProviderError,
    get_music_provider,
)

from .models import CreditAccount, CreditLedgerEntry, CreditOperationType
from .pricing import GenerationEstimate
from .serializers import (
    CreditAdminOperationSerializer,
    CreditTransferSerializer,
    DemoTopUpSerializer,
    GenerationEstimateSerializer,
    ProjectCreditBudgetSerializer,
)
from .services import (
    CreditServiceError,
    admin_transfer_credits,
    administer_credit_account,
    account_statistics,
    demo_top_up,
    get_or_create_account,
    generation_spending_statistics,
    list_admin_audit_events,
    list_entries,
    list_project_credit_budgets,
    set_project_credit_budget,
    validate_idempotency_key,
)
from w_craft_back.services.image_generation.errors import ImageProviderError
from w_craft_back.services.image_generation.registry import get_default_key
from w_craft_back.services.image_generation.routing import (
    build_routing_decision,
    routing_candidate_payloads,
)


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
        "isFrozen": account.is_frozen,
        "freezeReason": account.freeze_reason if account.is_frozen else "",
    }


def _admin_event_payload(event) -> dict:
    return {
        "id": str(event.id),
        "eventType": event.event_type,
        "amount": _credit(event.amount) if event.amount is not None else None,
        "reason": event.reason,
        "actor": event.actor.username if event.actor else None,
        "createdAt": event.created_at.isoformat(),
    }


def _project_budget_payload(snapshot: dict) -> dict:
    project = snapshot["project"]
    return {
        "projectId": project.pk,
        "projectTitle": project.title,
        "limit": _credit(snapshot["limit"]) if snapshot["limit"] is not None else None,
        "spent": _credit(snapshot["spent"]),
        "reserved": _credit(snapshot["reserved"]),
        "remaining": (
            _credit(snapshot["remaining"])
            if snapshot["remaining"] is not None
            else None
        ),
        "overLimit": snapshot["over_limit"],
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
        low_balance_threshold = Decimal(str(settings.CREDITS_LOW_BALANCE_THRESHOLD))
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
                    "transfersEnabled": bool(request.user.is_staff),
                    "adminWalletManagement": bool(request.user.is_staff),
                },
                "alerts": {
                    "lowBalance": (
                        not account.is_frozen
                        and account.available_balance <= low_balance_threshold
                    ),
                    "lowBalanceThreshold": _credit(low_balance_threshold),
                },
                "transferLimits": {
                    "perTransfer": _credit(
                        Decimal(str(settings.CREDITS_TRANSFER_MAX_AMOUNT))
                    ),
                    "rollingDay": _credit(
                        Decimal(str(settings.CREDITS_TRANSFER_DAILY_LIMIT))
                    ),
                    "rollingDayCount": settings.CREDITS_TRANSFER_DAILY_COUNT_LIMIT,
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
            if data["domain"] == "music":
                provider = get_music_provider()
                capabilities = provider.capabilities()
                if data["variantCount"] not in capabilities.variant_counts:
                    raise MusicProviderError(
                        "The selected music provider does not support "
                        "this variant count.",
                        code="MUSIC_CAPABILITY_UNSUPPORTED",
                        http_status=400,
                        retryable=False,
                    )
                pricing = provider.pricing(data["variantCount"])
                estimate = GenerationEstimate(
                    provider=provider.name,
                    model_key=provider.name,
                    model_name=provider.model_name,
                    currency="USD",
                    estimated_cost=pricing.estimated_cost,
                    reservation_amount=pricing.estimated_cost,
                    pricing_source=str(
                        pricing.snapshot.get("source") or provider.name
                    ),
                    prompt_tokens_estimate=0,
                    snapshot=dict(pricing.snapshot),
                )
                routing_mode = "manual"
                routing_reason = "music-provider"
                route_candidates = []
            elif data["domain"] == "model3d":
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
                routing_mode = "manual"
                routing_reason = "local-operation"
                route_candidates = []
            else:
                model_key = data["modelKey"] or (
                    getattr(
                        getattr(request.user, "profile", None),
                        "image_generation_model",
                        "",
                    )
                    or get_default_key()
                )
                decision = build_routing_decision(
                    mode=data["routingMode"],
                    requested_model=model_key,
                    operation=data["operation"],
                    variant_count=data["variantCount"],
                    prompt_length=data["promptLength"],
                    resolution=data["resolution"],
                )
                estimate = decision.primary.estimate
                routing_mode = decision.mode
                routing_reason = decision.reason
                route_candidates = routing_candidate_payloads(decision)
        except CreditServiceError as error:
            return _error_response(error)
        except ImageProviderError as error:
            return Response(
                {"code": error.code, "detail": error.message},
                status=error.http_status,
            )
        except MusicProviderError as error:
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
                "reservationAmount": _credit(
                    decision.reservation_amount
                    if data["domain"] not in {"music", "model3d"}
                    else estimate.reservation_amount
                ),
                "pricingSource": estimate.pricing_source,
                "costIsEstimate": True,
                "availableBalance": _credit(account.available_balance),
                "sufficientBalance": (
                    not account.is_frozen
                    and account.available_balance >= (
                        decision.reservation_amount
                        if data["domain"] not in {"music", "model3d"}
                        else estimate.reservation_amount
                    )
                ),
                "accountFrozen": account.is_frozen,
                "routingMode": routing_mode,
                "routingReason": routing_reason,
                "routeCandidates": route_candidates,
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


class CreditSpendingStatisticsView(APIView):
    def get(self, request):
        try:
            period_days = int(request.query_params.get("periodDays", 30))
            project_id_raw = request.query_params.get("projectId")
            project_id = int(project_id_raw) if project_id_raw else None
        except (TypeError, ValueError):
            return Response(
                {
                    "code": "INVALID_CREDIT_STATS_FILTER",
                    "detail": "periodDays and projectId must be integers.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if period_days not in {7, 30, 90, 365} or (
            project_id is not None and project_id <= 0
        ):
            return Response(
                {
                    "code": "INVALID_CREDIT_STATS_FILTER",
                    "detail": "Choose a supported period and project.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        account = get_or_create_account(request.user)
        stats = generation_spending_statistics(
            account,
            period_days=period_days,
            project_id=project_id,
        )
        return Response({
            "periodDays": stats["period_days"],
            "totalCharged": _credit(stats["total_charged"]),
            "jobCount": stats["job_count"],
            "byDomain": [
                {
                    "domain": row["domain"],
                    "charged": _credit(row["charged"]),
                    "jobCount": row["jobs"],
                }
                for row in stats["by_domain"]
            ],
            "byProject": [
                {
                    "projectId": row["project_id"],
                    "projectTitle": row["project__title"] or "Без проекта",
                    "charged": _credit(row["charged"]),
                    "jobCount": row["jobs"],
                }
                for row in stats["by_project"]
            ],
            "timeline": [
                {
                    "date": row["day"].isoformat(),
                    "charged": _credit(row["charged"]),
                    "jobCount": row["jobs"],
                }
                for row in stats["timeline"]
            ],
        })


class CreditAdminOperationView(APIView):
    def post(self, request):
        serializer = CreditAdminOperationSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            key = validate_idempotency_key(request.headers.get("Idempotency-Key"))
            result = administer_credit_account(
                actor=request.user,
                action=serializer.validated_data["action"],
                reason=serializer.validated_data["reason"],
                idempotency_key=key,
            )
        except CreditServiceError as error:
            return _error_response(error)
        return Response(
            {
                "account": _account_payload(result.account),
                "auditEvent": _admin_event_payload(result.event),
                "replayed": result.replayed,
            },
            status=(status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED),
        )


class CreditAdminAuditView(APIView):
    def get(self, request):
        if not request.user.is_staff:
            return Response(
                {
                    "code": "CREDIT_ADMIN_FORBIDDEN",
                    "detail": "Операция доступна только администратору.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        username = str(request.query_params.get("username") or "").strip()
        target = User.objects.filter(username=username).first()
        if target is None:
            return Response(
                {
                    "code": "CREDIT_RECIPIENT_NOT_FOUND",
                    "detail": "Пользователь с таким логином не найден.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        account = get_or_create_account(target)
        return Response({
            "username": target.username,
            "account": _account_payload(account),
            "items": [
                _admin_event_payload(event)
                for event in list_admin_audit_events(account)
            ],
        })


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
                "account": _account_payload(get_or_create_account(request.user)),
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
            admin_result = admin_transfer_credits(
                actor=request.user,
                sender_username=serializer.validated_data["senderUsername"],
                recipient_username=serializer.validated_data["recipientUsername"],
                amount=serializer.validated_data["amount"],
                reason=serializer.validated_data["reason"],
                idempotency_key=key,
            )
        except CreditServiceError as error:
            return _error_response(error)

        result = admin_result.transfer

        response_status = (
            status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
        )
        return Response(
            {
                "account": _account_payload(result.sender_entry.account),
                "transfer": {
                    "id": str(result.sender_entry.correlation_id),
                    "amount": _credit(-result.sender_entry.available_delta),
                    "sender": result.sender_entry.account.user.username,
                    "recipient": _counterparty_payload(result.sender_entry),
                    "note": result.sender_entry.description,
                    "createdAt": result.sender_entry.created_at.isoformat(),
                },
                "auditEvent": _admin_event_payload(admin_result.event),
                "replayed": result.replayed,
            },
            status=response_status,
        )


class ProjectCreditBudgetListView(APIView):
    def get(self, request):
        return Response({
            "items": [
                _project_budget_payload(snapshot)
                for snapshot in list_project_credit_budgets(request.user)
            ],
        })


class ProjectCreditBudgetDetailView(APIView):
    def patch(self, request, project_id: int):
        serializer = ProjectCreditBudgetSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            snapshot = set_project_credit_budget(
                actor=request.user,
                project_id=project_id,
                limit=serializer.validated_data["limit"],
            )
        except CreditServiceError as error:
            return _error_response(error)
        return Response(_project_budget_payload(snapshot))
