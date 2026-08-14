from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import DecimalField, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import CreditAccount, CreditLedgerEntry, CreditOperationType


ZERO = Decimal("0.00")
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


@dataclass(frozen=True)
class TopUpResult:
    entry: CreditLedgerEntry
    replayed: bool


@dataclass(frozen=True)
class TransferResult:
    sender_entry: CreditLedgerEntry
    recipient_entry: CreditLedgerEntry
    replayed: bool


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
    decimal_output = DecimalField(max_digits=18, decimal_places=2)
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
        spent=Coalesce(
            Sum(
                "reserved_delta",
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
        "spent": -aggregates["spent"],
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
