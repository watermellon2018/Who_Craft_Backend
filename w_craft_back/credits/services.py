from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import DecimalField, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (
    CreditAccount,
    CreditLedgerEntry,
    CreditOperationType,
    GenerationCharge,
    GenerationChargeStatus,
)


MONEY_QUANTUM = Decimal("0.000001")
ZERO = Decimal("0.000000")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


class CreditServiceError(Exception):
    code = "CREDIT_ERROR"
    http_status = 400

    def __init__(self, message: str, *, fields: dict[str, list[str]] | None = None):
        super().__init__(message)
        self.message = message
        self.fields = fields


class InvalidIdempotencyKey(CreditServiceError):
    code = "INVALID_IDEMPOTENCY_KEY"


class IdempotencyConflict(CreditServiceError):
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409


class RecipientNotFound(CreditServiceError):
    code = "CREDIT_RECIPIENT_NOT_FOUND"
    http_status = 404


class SelfTransfer(CreditServiceError):
    code = "CREDIT_SELF_TRANSFER"


class InsufficientCredits(CreditServiceError):
    code = "INSUFFICIENT_CREDITS"
    http_status = 409


class GenerationPriceUnavailable(CreditServiceError):
    code = "GENERATION_PRICE_UNAVAILABLE"
    http_status = 503


@dataclass(frozen=True)
class TopUpResult:
    entry: CreditLedgerEntry
    replayed: bool


@dataclass(frozen=True)
class TransferResult:
    sender_entry: CreditLedgerEntry
    recipient_entry: CreditLedgerEntry
    replayed: bool


@dataclass(frozen=True)
class GenerationSettlementResult:
    charge: GenerationCharge
    replayed: bool


def money(value: Any) -> Decimal:
    """Normalize provider USD values to the wallet's six-decimal precision."""

    try:
        amount = Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CreditServiceError("Некорректная стоимость генерации.") from exc
    if not amount.is_finite() or amount < ZERO:
        raise CreditServiceError("Стоимость генерации не может быть отрицательной.")
    return amount


def validate_idempotency_key(raw_key: str | None) -> str:
    """Validate the public idempotency key shared by credit mutations."""

    key = (raw_key or "").strip()
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise InvalidIdempotencyKey(
            "Idempotency-Key must be 8-64 characters using letters, digits, "
            "'.', '_', ':' or '-'."
        )
    return key


def get_or_create_account(user: User) -> CreditAccount:
    account, _ = CreditAccount.objects.get_or_create(user=user)
    return account


def _request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_replay(
    entry: CreditLedgerEntry,
    *,
    operation_type: str,
    request_hash: str,
) -> None:
    if (
        entry.operation_type != operation_type
        or entry.metadata.get("request_hash") != request_hash
    ):
        raise IdempotencyConflict(
            "This Idempotency-Key was already used for a different credit operation."
        )


def _locked_accounts(*users: User) -> dict[int, CreditAccount]:
    user_ids = sorted({user.pk for user in users})
    CreditAccount.objects.bulk_create(
        [CreditAccount(user_id=user_id) for user_id in user_ids],
        ignore_conflicts=True,
    )
    accounts = (
        CreditAccount.objects.select_for_update()
        .filter(user_id__in=user_ids)
        .order_by("pk")
    )
    return {account.user_id: account for account in accounts}


def _generation_metadata(charge: GenerationCharge, **extra: Any) -> dict[str, Any]:
    return {
        "generationChargeId": str(charge.id),
        "domain": charge.domain,
        "jobId": charge.job_id,
        "provider": charge.provider,
        "model": charge.model_name,
        **extra,
    }


def _generation_entry(
    *,
    charge: GenerationCharge,
    operation_type: str,
    available_delta: Decimal,
    reserved_delta: Decimal,
    description: str,
    suffix: str,
    metadata: dict[str, Any],
) -> CreditLedgerEntry | None:
    if available_delta == ZERO and reserved_delta == ZERO:
        return None
    account = charge.account
    return CreditLedgerEntry.objects.create(
        account=account,
        operation_type=operation_type,
        available_delta=available_delta,
        reserved_delta=reserved_delta,
        available_balance_after=account.available_balance,
        reserved_balance_after=account.reserved_balance,
        correlation_id=charge.id,
        idempotency_key=f"generation:{charge.id}:{suffix}",
        description=description,
        metadata=_generation_metadata(charge, **metadata),
    )


@transaction.atomic
def reserve_generation(
    *,
    user: User,
    domain: str,
    job_id: str,
    provider: str,
    model_name: str,
    estimated_cost: Decimal,
    reservation_amount: Decimal,
    pricing_snapshot: dict[str, Any] | None = None,
) -> GenerationSettlementResult:
    """Reserve credits once for a durable job before it can reach a provider."""

    estimated = money(estimated_cost)
    reserved = money(reservation_amount)
    account = _locked_accounts(user)[user.pk]
    existing = GenerationCharge.objects.select_for_update().filter(
        domain=domain,
        job_id=str(job_id),
    ).first()
    if existing is not None:
        if existing.account_id != account.id:
            raise IdempotencyConflict("Generation job is owned by another account.")
        return GenerationSettlementResult(charge=existing, replayed=True)
    if account.available_balance < reserved:
        raise InsufficientCredits(
            "Недостаточно кредитов для запуска генерации. Пополните кошелек."
        )
    charge = GenerationCharge.objects.create(
        account=account,
        domain=str(domain)[:32],
        job_id=str(job_id)[:64],
        provider=str(provider or "")[:128],
        model_name=str(model_name or "")[:300],
        estimated_cost=estimated,
        reserved_amount=reserved,
        pricing_snapshot=dict(pricing_snapshot or {}),
    )
    if reserved:
        account.available_balance -= reserved
        account.reserved_balance += reserved
        account.save(update_fields=["available_balance", "reserved_balance", "updated_at"])
        _generation_entry(
            charge=charge,
            operation_type=CreditOperationType.RESERVE,
            available_delta=-reserved,
            reserved_delta=reserved,
            description="Резерв на генерацию",
            suffix="reserve",
            metadata={"estimatedCost": str(estimated), "reservedAmount": str(reserved)},
        )
    return GenerationSettlementResult(charge=charge, replayed=False)


@transaction.atomic
def capture_generation(
    *,
    domain: str,
    job_id: str,
    actual_cost: Decimal,
    provider_usage: dict[str, Any] | None = None,
    cost_is_estimate: bool = False,
) -> GenerationSettlementResult | None:
    """Capture actual provider cost once and release unused reservation."""

    charge = GenerationCharge.objects.select_for_update().select_related(
        "account"
    ).filter(domain=domain, job_id=str(job_id)).first()
    if charge is None:
        return None
    if charge.status != GenerationChargeStatus.RESERVED:
        return GenerationSettlementResult(charge=charge, replayed=True)
    account = CreditAccount.objects.select_for_update().get(pk=charge.account_id)
    charge.account = account
    actual = money(actual_cost)
    from_reserved = min(actual, charge.reserved_amount)
    release_amount = charge.reserved_amount - from_reserved
    extra_required = actual - from_reserved
    extra_charged = min(extra_required, account.available_balance)
    charged = from_reserved + extra_charged
    uncovered = actual - charged

    account.reserved_balance -= charge.reserved_amount
    account.available_balance += release_amount - extra_charged
    account.save(update_fields=["available_balance", "reserved_balance", "updated_at"])
    _generation_entry(
        charge=charge,
        operation_type=CreditOperationType.CAPTURE,
        available_delta=-extra_charged,
        reserved_delta=-from_reserved,
        description="Списание за генерацию",
        suffix="capture",
        metadata={
            "actualCost": str(actual),
            "chargedAmount": str(charged),
            "costIsEstimate": bool(cost_is_estimate),
        },
    )
    _generation_entry(
        charge=charge,
        operation_type=CreditOperationType.RELEASE,
        available_delta=release_amount,
        reserved_delta=-release_amount,
        description="Возврат неиспользованного резерва",
        suffix="remainder",
        metadata={"releasedAmount": str(release_amount)},
    )
    charge.actual_cost = actual
    charge.charged_amount = charged
    charge.uncovered_cost = uncovered
    charge.cost_is_estimate = bool(cost_is_estimate)
    charge.provider_usage = dict(provider_usage or {})
    charge.status = GenerationChargeStatus.CAPTURED
    charge.settled_at = timezone.now()
    charge.save()
    return GenerationSettlementResult(charge=charge, replayed=False)


@transaction.atomic
def release_generation(
    *,
    domain: str,
    job_id: str,
    reason: str = "",
) -> GenerationSettlementResult | None:
    """Return an unfinished job's reservation exactly once."""

    charge = GenerationCharge.objects.select_for_update().select_related(
        "account"
    ).filter(domain=domain, job_id=str(job_id)).first()
    if charge is None:
        return None
    if charge.status != GenerationChargeStatus.RESERVED:
        return GenerationSettlementResult(charge=charge, replayed=True)
    account = CreditAccount.objects.select_for_update().get(pk=charge.account_id)
    charge.account = account
    amount = charge.reserved_amount
    if amount:
        account.reserved_balance -= amount
        account.available_balance += amount
        account.save(update_fields=["available_balance", "reserved_balance", "updated_at"])
        _generation_entry(
            charge=charge,
            operation_type=CreditOperationType.RELEASE,
            available_delta=amount,
            reserved_delta=-amount,
            description="Отмена резерва генерации",
            suffix="release",
            metadata={"reason": str(reason)[:200]},
        )
    charge.status = GenerationChargeStatus.RELEASED
    charge.settled_at = timezone.now()
    charge.save(update_fields=["status", "settled_at"])
    return GenerationSettlementResult(charge=charge, replayed=False)


def capture_provider_generation(
    *,
    domain: str,
    job_id: str,
    provider: Any,
) -> GenerationSettlementResult | None:
    """Settle a provider call from its sanitized usage, with explicit fallback."""

    from w_craft_back.services.image_generation.usage import provider_usage_snapshot

    usage = provider_usage_snapshot(provider)
    charge = GenerationCharge.objects.filter(
        domain=domain,
        job_id=str(job_id),
    ).first()
    if charge is None:
        return None
    raw_cost = usage.get("costUsd")
    cost_is_estimate = raw_cost is None
    actual_cost = charge.estimated_cost if cost_is_estimate else money(raw_cost)
    if cost_is_estimate:
        usage = {**usage, "costSource": "enqueue-estimate"}
    return capture_generation(
        domain=domain,
        job_id=str(job_id),
        actual_cost=actual_cost,
        provider_usage=usage,
        cost_is_estimate=cost_is_estimate,
    )


def generation_charge_payload(domain: str, job_id: str) -> dict[str, Any] | None:
    charge = GenerationCharge.objects.filter(
        domain=domain,
        job_id=str(job_id),
    ).first()
    if charge is None:
        return None
    return {
        "status": charge.status,
        "currency": charge.currency,
        "estimatedCost": format(charge.estimated_cost, "f"),
        "reservedAmount": format(charge.reserved_amount, "f"),
        "actualCost": (
            format(charge.actual_cost, "f")
            if charge.actual_cost is not None
            else None
        ),
        "chargedAmount": format(charge.charged_amount, "f"),
        "uncoveredCost": format(charge.uncovered_cost, "f"),
        "costIsEstimate": charge.cost_is_estimate,
        "provider": charge.provider,
        "model": charge.model_name,
    }


@transaction.atomic
def demo_top_up(
    *,
    user: User,
    amount: Decimal,
    idempotency_key: str,
) -> TopUpResult:
    """Add demo credits exactly once for a user and request key."""

    request_hash = _request_hash({"amount": str(amount)})
    account = _locked_accounts(user)[user.pk]
    existing = account.ledger_entries.filter(
        idempotency_key=idempotency_key,
    ).first()
    if existing is not None:
        _validate_replay(
            existing,
            operation_type=CreditOperationType.DEMO_TOP_UP,
            request_hash=request_hash,
        )
        return TopUpResult(entry=existing, replayed=True)

    account.available_balance += amount
    account.save(update_fields=["available_balance", "updated_at"])
    entry = CreditLedgerEntry.objects.create(
        account=account,
        operation_type=CreditOperationType.DEMO_TOP_UP,
        available_delta=amount,
        reserved_delta=ZERO,
        available_balance_after=account.available_balance,
        reserved_balance_after=account.reserved_balance,
        idempotency_key=idempotency_key,
        description="Демонстрационное пополнение",
        metadata={"request_hash": request_hash, "demo": True},
    )
    return TopUpResult(entry=entry, replayed=False)


def _recipient_by_username(username: str) -> User:
    recipient = User.objects.filter(username=username, is_active=True).first()
    if recipient is None:
        raise RecipientNotFound("Пользователь с таким логином не найден.")
    return recipient


@transaction.atomic
def transfer_credits(
    *,
    sender: User,
    recipient_username: str,
    amount: Decimal,
    note: str,
    idempotency_key: str,
) -> TransferResult:
    """Atomically move available credits between two Craft accounts."""

    recipient = _recipient_by_username(recipient_username)
    if recipient.pk == sender.pk:
        raise SelfTransfer("Нельзя переводить кредиты самому себе.")

    request_hash = _request_hash(
        {
            "amount": str(amount),
            "note": note,
            "recipient_id": recipient.pk,
        }
    )
    accounts = _locked_accounts(sender, recipient)
    sender_account = accounts[sender.pk]
    recipient_account = accounts[recipient.pk]

    existing = sender_account.ledger_entries.filter(
        idempotency_key=idempotency_key,
    ).first()
    if existing is not None:
        _validate_replay(
            existing,
            operation_type=CreditOperationType.TRANSFER_OUT,
            request_hash=request_hash,
        )
        recipient_entry = CreditLedgerEntry.objects.get(
            account=recipient_account,
            correlation_id=existing.correlation_id,
            operation_type=CreditOperationType.TRANSFER_IN,
        )
        return TransferResult(
            sender_entry=existing,
            recipient_entry=recipient_entry,
            replayed=True,
        )

    if sender_account.available_balance < amount:
        raise InsufficientCredits("Недостаточно доступных кредитов для перевода.")

    sender_account.available_balance -= amount
    recipient_account.available_balance += amount
    sender_account.save(update_fields=["available_balance", "updated_at"])
    recipient_account.save(update_fields=["available_balance", "updated_at"])

    correlation_id = uuid.uuid4()
    description = note or "Перевод кредитов"
    common_metadata = {"request_hash": request_hash}
    sender_entry = CreditLedgerEntry.objects.create(
        account=sender_account,
        operation_type=CreditOperationType.TRANSFER_OUT,
        available_delta=-amount,
        reserved_delta=ZERO,
        available_balance_after=sender_account.available_balance,
        reserved_balance_after=sender_account.reserved_balance,
        correlation_id=correlation_id,
        counterparty=recipient,
        idempotency_key=idempotency_key,
        description=description,
        metadata=common_metadata,
    )
    recipient_entry = CreditLedgerEntry.objects.create(
        account=recipient_account,
        operation_type=CreditOperationType.TRANSFER_IN,
        available_delta=amount,
        reserved_delta=ZERO,
        available_balance_after=recipient_account.available_balance,
        reserved_balance_after=recipient_account.reserved_balance,
        correlation_id=correlation_id,
        counterparty=sender,
        # The idempotency key belongs to the sender's request namespace. The
        # linked incoming entry is found by correlation_id and must not consume
        # an unrelated key in the recipient's account.
        idempotency_key=None,
        description=description,
        metadata=common_metadata,
    )
    return TransferResult(
        sender_entry=sender_entry,
        recipient_entry=recipient_entry,
        replayed=False,
    )


def account_statistics(
    account: CreditAccount,
    *,
    period_days: int = 30,
) -> dict[str, Decimal | int]:
    """Return auditable credit movement totals for the recent period."""

    since = timezone.now() - timedelta(days=period_days)
    decimal_output = DecimalField(max_digits=18, decimal_places=6)
    aggregates = account.ledger_entries.filter(created_at__gte=since).aggregate(
        received=Coalesce(
            Sum(
                "available_delta",
                filter=Q(
                    operation_type__in=[
                        CreditOperationType.DEMO_TOP_UP,
                        CreditOperationType.TRANSFER_IN,
                    ]
                ),
            ),
            ZERO,
            output_field=decimal_output,
        ),
        sent=Coalesce(
            Sum(
                "available_delta",
                filter=Q(operation_type=CreditOperationType.TRANSFER_OUT),
            ),
            ZERO,
            output_field=decimal_output,
        ),
        spent_reserved=Coalesce(
            Sum(
                "reserved_delta",
                filter=Q(operation_type=CreditOperationType.CAPTURE),
            ),
            ZERO,
            output_field=decimal_output,
        ),
        spent_available=Coalesce(
            Sum(
                "available_delta",
                filter=Q(operation_type=CreditOperationType.CAPTURE),
            ),
            ZERO,
            output_field=decimal_output,
        ),
        refunded=Coalesce(
            Sum(
                "available_delta",
                filter=Q(operation_type=CreditOperationType.REFUND),
            ),
            ZERO,
            output_field=decimal_output,
        ),
    )
    return {
        "period_days": period_days,
        "received": aggregates["received"],
        "sent": -aggregates["sent"],
        "spent": -aggregates["spent_reserved"] - aggregates["spent_available"],
        "refunded": aggregates["refunded"],
    }


def list_entries(
    account: CreditAccount,
    *,
    limit: int,
    offset: int,
    operation_type: str | None = None,
):
    queryset = account.ledger_entries.select_related(
        "counterparty",
        "counterparty__profile",
    )
    if operation_type:
        queryset = queryset.filter(operation_type=operation_type)
    return queryset[offset:offset + limit]
