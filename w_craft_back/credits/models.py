from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class CreditOperationType(models.TextChoices):
    DEMO_TOP_UP = "demo_top_up", "Demo top-up"
    TRANSFER_OUT = "transfer_out", "Transfer sent"
    TRANSFER_IN = "transfer_in", "Transfer received"
    RESERVE = "reserve", "Credits reserved"
    CAPTURE = "capture", "Reserved credits charged"
    RELEASE = "release", "Reserved credits released"
    REFUND = "refund", "Credits refunded"
    ADJUSTMENT = "adjustment", "Administrative adjustment"


class ImmutableCreditLedgerQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Credit ledger entries are immutable.")

    def delete(self):
        raise ValidationError("Credit ledger entries are append-only.")


class CreditAccount(models.Model):
    """Current cached balances for one Craft user.

    The ledger is the audit trail. These balances are updated in the same
    database transaction as every ledger entry so reads remain inexpensive.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="credit_account",
    )
    available_balance = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        default=0,
    )
    reserved_balance = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        default=0,
    )
    is_frozen = models.BooleanField(default=False, db_index=True)
    freeze_reason = models.CharField(max_length=255, blank=True, default="")
    frozen_at = models.DateTimeField(null=True, blank=True)
    frozen_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="frozen_credit_accounts",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "credit_accounts"
        constraints = [
            models.CheckConstraint(
                condition=Q(available_balance__gte=0),
                name="credit_account_available_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(reserved_balance__gte=0),
                name="credit_account_reserved_nonnegative",
            ),
        ]

    @property
    def total_balance(self) -> Decimal:
        return self.available_balance + self.reserved_balance

    def __str__(self) -> str:
        return f"{self.user.username}: {self.available_balance} credits"


class CreditLedgerEntry(models.Model):
    """Append-only balance change recorded for one account."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        CreditAccount,
        on_delete=models.CASCADE,
        related_name="ledger_entries",
    )
    operation_type = models.CharField(
        max_length=32,
        choices=CreditOperationType.choices,
    )
    available_delta = models.DecimalField(max_digits=18, decimal_places=6)
    reserved_delta = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    available_balance_after = models.DecimalField(max_digits=18, decimal_places=6)
    reserved_balance_after = models.DecimalField(max_digits=18, decimal_places=6)
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    counterparty = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="credit_ledger_counterparties",
        null=True,
        blank=True,
    )
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    description = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableCreditLedgerQuerySet.as_manager()

    class Meta:
        db_table = "credit_ledger_entries"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["account", "created_at"],
                name="credit_ledger_acct_created",
            ),
            models.Index(
                fields=["account", "operation_type", "created_at"],
                name="credit_ledger_type_created",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="credit_ledger_account_idempotency_unique",
            ),
            models.CheckConstraint(
                condition=~(
                    Q(available_delta=0)
                    & Q(reserved_delta=0)
                ),
                name="credit_ledger_nonzero_delta",
            ),
            models.CheckConstraint(
                condition=Q(available_balance_after__gte=0),
                name="credit_ledger_available_after_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(reserved_balance_after__gte=0),
                name="credit_ledger_reserved_after_nonnegative",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Credit ledger entries are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Credit ledger entries are append-only.")

    def __str__(self) -> str:
        return f"{self.operation_type}: {self.available_delta}"


class GenerationChargeStatus(models.TextChoices):
    RESERVED = "reserved", "Reserved"
    CAPTURED = "captured", "Captured"
    RELEASED = "released", "Released"


class GenerationCharge(models.Model):
    """One idempotent wallet settlement for one durable generation job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        CreditAccount,
        on_delete=models.CASCADE,
        related_name="generation_charges",
    )
    project = models.ForeignKey(
        "w_craft_back.Project",
        on_delete=models.SET_NULL,
        related_name="generation_charges",
        null=True,
        blank=True,
    )
    domain = models.CharField(max_length=32)
    operation = models.CharField(max_length=32, default="generate")
    routing_mode = models.CharField(max_length=16, default="manual")
    job_id = models.CharField(max_length=64)
    provider = models.CharField(max_length=128, blank=True, default="")
    model_name = models.CharField(max_length=300, blank=True, default="")
    currency = models.CharField(max_length=3, default="USD")
    estimated_cost = models.DecimalField(max_digits=18, decimal_places=6)
    reserved_amount = models.DecimalField(max_digits=18, decimal_places=6)
    actual_cost = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        null=True,
        blank=True,
    )
    charged_amount = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        default=0,
    )
    uncovered_cost = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        default=0,
    )
    cost_is_estimate = models.BooleanField(default=True)
    status = models.CharField(
        max_length=16,
        choices=GenerationChargeStatus.choices,
        default=GenerationChargeStatus.RESERVED,
    )
    pricing_snapshot = models.JSONField(default=dict, blank=True)
    provider_usage = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "generation_charges"
        constraints = [
            models.UniqueConstraint(
                fields=["domain", "job_id"],
                name="generation_charge_job_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(estimated_cost__gte=0)
                    & Q(reserved_amount__gte=0)
                    & Q(charged_amount__gte=0)
                    & Q(uncovered_cost__gte=0)
                ),
                name="generation_charge_amounts_nonnegative",
            ),
        ]
        indexes = [
            models.Index(
                fields=["account", "created_at"],
                name="generation_charge_acct_created",
            ),
            models.Index(
                fields=["account", "settled_at"],
                name="generation_charge_acct_settled",
            ),
            models.Index(
                fields=["project", "settled_at"],
                name="generation_charge_proj_settled",
            ),
        ]


class CreditAdminEventType(models.TextChoices):
    ADJUSTMENT = "adjustment", "Balance adjustment"
    REFUND = "refund", "Manual refund"
    FREEZE = "freeze", "Wallet frozen"
    UNFREEZE = "unfreeze", "Wallet unfrozen"
    TRANSFER = "transfer", "Administrative transfer"


class ImmutableCreditAdminAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Credit administration audit events are immutable.")

    def delete(self):
        raise ValidationError("Credit administration audit is append-only.")


class CreditAdminAuditEvent(models.Model):
    """Append-only audit record for every manual wallet operation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        CreditAccount,
        on_delete=models.CASCADE,
        related_name="admin_audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="credit_admin_audit_events",
        null=True,
        blank=True,
    )
    event_type = models.CharField(
        max_length=16,
        choices=CreditAdminEventType.choices,
    )
    amount = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        null=True,
        blank=True,
    )
    reason = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableCreditAdminAuditQuerySet.as_manager()

    class Meta:
        db_table = "credit_admin_audit_events"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "idempotency_key"],
                name="credit_admin_audit_account_idem_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["account", "created_at"],
                name="credit_admin_acct_created",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Credit administration audit events are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Credit administration audit is append-only.")
