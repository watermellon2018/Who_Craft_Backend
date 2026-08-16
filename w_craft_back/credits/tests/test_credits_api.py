from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.credits.models import (
    CreditAccount,
    CreditAdminAuditEvent,
    CreditLedgerEntry,
    CreditOperationType,
    GenerationCharge,
    GenerationChargeStatus,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.credits.services import (
    InsufficientCredits,
    ProjectCreditBudgetExceeded,
    capture_generation,
    capture_provider_generation,
    demo_top_up,
    release_generation,
    reserve_generation,
    transfer_credits,
)


class CreditsApiTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="alice",
            password="password",
            is_staff=True,
        )
        self.recipient = User.objects.create_user(username="test", password="12345678")
        self.token = UserKey.objects.create(user=self.user).key
        self.auth = {"HTTP_X_USER_TOKEN": self.token}

    def _top_up(self, amount: str = "100.00", key: str = "top-up-key-0001"):
        return self.client.post(
            reverse("credit-demo-top-up"),
            {"amount": amount},
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
            **self.auth,
        )

    def test_summary_requires_authentication(self):
        response = self.client.get(reverse("credit-summary"))

        self.assertEqual(response.status_code, 401)

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_summary_creates_zero_account_and_reports_capabilities(self):
        response = self.client.get(reverse("credit-summary"), **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["account"],
            {
                "availableBalance": "0.00",
                "reservedBalance": "0.00",
                "totalBalance": "0.00",
                "isFrozen": False,
                "freezeReason": "",
            },
        )
        self.assertTrue(response.json()["capabilities"]["demoTopUpEnabled"])
        self.assertTrue(CreditAccount.objects.filter(user=self.user).exists())

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_demo_top_up_is_idempotent(self):
        created = self._top_up()
        replay = self._top_up()

        self.assertEqual(created.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertFalse(created.json()["replayed"])
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(
            created.json()["transaction"]["id"],
            replay.json()["transaction"]["id"],
        )
        account = CreditAccount.objects.get(user=self.user)
        self.assertEqual(account.available_balance, Decimal("100.00"))
        self.assertEqual(account.ledger_entries.count(), 1)

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_demo_top_up_rejects_reused_key_with_different_amount(self):
        self._top_up()

        response = self._top_up(amount="200.00")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED")

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=False)
    def test_demo_top_up_can_be_disabled(self):
        response = self._top_up()

        self.assertEqual(response.status_code, 403)
        self.assertFalse(CreditAccount.objects.filter(user=self.user).exists())

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_demo_top_up_requires_valid_amount_and_idempotency_key(self):
        invalid_amount = self._top_up(amount="0")
        missing_key = self.client.post(
            reverse("credit-demo-top-up"),
            {"amount": "10.00"},
            format="json",
            **self.auth,
        )

        self.assertEqual(invalid_amount.status_code, 400)
        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json()["error"]["code"], "INVALID_IDEMPOTENCY_KEY")

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_transfer_moves_credits_and_creates_linked_entries(self):
        self._top_up()

        response = self.client.post(
            reverse("credit-transfer"),
            {
                "senderUsername": "alice",
                "recipientUsername": "test",
                "amount": "35.50",
                "reason": "На раскадровку",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-key-0001",
            **self.auth,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["account"]["availableBalance"], "64.50")
        self.assertEqual(response.json()["transfer"]["recipient"]["username"], "test")
        sender = CreditAccount.objects.get(user=self.user)
        recipient = CreditAccount.objects.get(user=self.recipient)
        self.assertEqual(sender.available_balance, Decimal("64.50"))
        self.assertEqual(recipient.available_balance, Decimal("35.50"))
        transfer_entries = CreditLedgerEntry.objects.filter(
            correlation_id=response.json()["transfer"]["id"]
        )
        self.assertEqual(transfer_entries.count(), 2)
        self.assertSetEqual(
            set(transfer_entries.values_list("operation_type", flat=True)),
            {"transfer_out", "transfer_in"},
        )

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_transfer_replay_does_not_move_credits_twice(self):
        self._top_up()
        payload = {
            "senderUsername": "alice",
            "recipientUsername": "test",
            "amount": "25.00",
            "reason": "Командный перевод",
        }

        created = self.client.post(
            reverse("credit-transfer"),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-key-0002",
            **self.auth,
        )
        replay = self.client.post(
            reverse("credit-transfer"),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-key-0002",
            **self.auth,
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(
            CreditAccount.objects.get(user=self.user).available_balance,
            Decimal("75.00"),
        )
        self.assertEqual(
            CreditAccount.objects.get(user=self.recipient).available_balance,
            Decimal("25.00"),
        )

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_transfer_key_does_not_conflict_with_recipient_key_namespace(self):
        self._top_up()
        demo_top_up(
            user=self.recipient,
            amount=Decimal("10.00"),
            idempotency_key="shared-client-key",
        )

        response = self.client.post(
            reverse("credit-transfer"),
            {
                "senderUsername": "alice",
                "recipientUsername": "test",
                "amount": "1.00",
                "reason": "Командный перевод",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="shared-client-key",
            **self.auth,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            CreditAccount.objects.get(user=self.recipient).available_balance,
            Decimal("11.00"),
        )

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_transfer_rejects_insufficient_available_balance(self):
        self._top_up(amount="20.00")
        account = CreditAccount.objects.get(user=self.user)
        account.reserved_balance = Decimal("100.00")
        account.save(update_fields=["reserved_balance", "updated_at"])

        response = self.client.post(
            reverse("credit-transfer"),
            {
                "senderUsername": "alice",
                "recipientUsername": "test",
                "amount": "21.00",
                "reason": "Командный перевод",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-key-0003",
            **self.auth,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "INSUFFICIENT_CREDITS")
        self.assertFalse(CreditAccount.objects.filter(user=self.recipient).exists())

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_transfer_rejects_self_unknown_and_case_mismatched_recipient(self):
        self._top_up()

        self_transfer = self.client.post(
            reverse("credit-transfer"),
            {
                "senderUsername": "alice",
                "recipientUsername": "alice",
                "amount": "1.00",
                "reason": "Проверка",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-key-self",
            **self.auth,
        )
        unknown = self.client.post(
            reverse("credit-transfer"),
            {
                "senderUsername": "alice",
                "recipientUsername": "nobody",
                "amount": "1.00",
                "reason": "Проверка",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-key-unknown",
            **self.auth,
        )
        wrong_case = self.client.post(
            reverse("credit-transfer"),
            {
                "senderUsername": "alice",
                "recipientUsername": "Test",
                "amount": "1.00",
                "reason": "Проверка",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-key-case",
            **self.auth,
        )

        self.assertEqual(self_transfer.status_code, 400)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(wrong_case.status_code, 404)

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_history_and_statistics_match_ledger_without_exposing_metadata(self):
        self._top_up(amount="100.00")
        self.client.post(
            reverse("credit-transfer"),
            {
                "senderUsername": "alice",
                "recipientUsername": "test",
                "amount": "25.00",
                "reason": "Командный перевод",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-key-0004",
            **self.auth,
        )

        summary = self.client.get(reverse("credit-summary"), **self.auth)
        history = self.client.get(
            reverse("credit-history"),
            {"limit": 1, "offset": 0},
            **self.auth,
        )

        self.assertEqual(summary.json()["stats"]["received"], "100.00")
        self.assertEqual(summary.json()["stats"]["sent"], "25.00")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["total"], 2)
        self.assertEqual(history.json()["nextOffset"], 1)
        item = history.json()["items"][0]
        self.assertNotIn("metadata", item)
        self.assertNotIn("idempotencyKey", item)

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_statistics_report_refunds_separately_from_received_credits(self):
        self._top_up(amount="100.00")
        account = CreditAccount.objects.get(user=self.user)
        account.available_balance += Decimal("5.00")
        account.save(update_fields=["available_balance", "updated_at"])
        CreditLedgerEntry.objects.create(
            account=account,
            operation_type=CreditOperationType.REFUND,
            available_delta=Decimal("5.00"),
            reserved_delta=Decimal("0.00"),
            available_balance_after=account.available_balance,
            reserved_balance_after=account.reserved_balance,
            description="Provider refund",
        )

        response = self.client.get(reverse("credit-summary"), **self.auth)

        self.assertEqual(response.json()["stats"]["received"], "100.00")
        self.assertEqual(response.json()["stats"]["refunded"], "5.00")

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_ledger_entries_reject_model_updates_and_deletes(self):
        self._top_up()
        entry = CreditLedgerEntry.objects.get(account__user=self.user)

        entry.description = "Changed"
        with self.assertRaises(ValidationError):
            entry.save()
        with self.assertRaises(ValidationError):
            entry.delete()
        with self.assertRaises(ValidationError):
            CreditLedgerEntry.objects.filter(pk=entry.pk).update(
                description="Changed"
            )
        with self.assertRaises(ValidationError):
            CreditLedgerEntry.objects.filter(pk=entry.pk).delete()

    def test_history_validates_pagination_and_operation_type(self):
        bad_limit = self.client.get(
            reverse("credit-history"),
            {"limit": 1000},
            **self.auth,
        )
        bad_type = self.client.get(
            reverse("credit-history"),
            {"operationType": "unknown"},
            **self.auth,
        )

        self.assertEqual(bad_limit.status_code, 400)
        self.assertEqual(bad_type.status_code, 400)

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_mutations_reject_json_array_payloads_with_400(self):
        top_up = self.client.post(
            reverse("credit-demo-top-up"),
            [],
            format="json",
            HTTP_IDEMPOTENCY_KEY="array-key-top-up",
            **self.auth,
        )
        transfer = self.client.post(
            reverse("credit-transfer"),
            [],
            format="json",
            HTTP_IDEMPOTENCY_KEY="array-key-transfer",
            **self.auth,
        )

        self.assertEqual(top_up.status_code, 400)
        self.assertEqual(transfer.status_code, 400)

    def test_generation_estimate_uses_original_provider_price_and_balance(self):
        CreditAccount.objects.create(
            user=self.user,
            available_balance=Decimal("1.000000"),
        )

        response = self.client.post(
            reverse("credit-generation-estimate"),
            {
                "domain": "character",
                "operation": "generate",
                "modelKey": "gemini-flash-image",
                "variantCount": 1,
                "promptLength": 0,
            },
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["estimatedCost"], "0.039")
        self.assertEqual(response.json()["reservationAmount"], "0.039")
        self.assertEqual(response.json()["pricingSource"], "google")
        self.assertTrue(response.json()["sufficientBalance"])

    @override_settings(
        CREDITS_DEMO_TOP_UP_ENABLED=True,
        CREDITS_TRANSFER_MAX_AMOUNT="10.00",
        CREDITS_TRANSFER_DAILY_LIMIT="15.00",
        CREDITS_TRANSFER_DAILY_COUNT_LIMIT=2,
    )
    def test_transfer_limits_and_frozen_wallet_are_enforced(self):
        self._top_up(amount="100.00")
        too_large = self.client.post(
            reverse("credit-transfer"),
            {
                "senderUsername": "alice",
                "recipientUsername": "test",
                "amount": "11.00",
                "reason": "Проверка лимита",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-limit-large",
            **self.auth,
        )
        self.assertEqual(too_large.status_code, 429)

        account = CreditAccount.objects.get(user=self.user)
        account.is_frozen = True
        account.freeze_reason = "Security review"
        account.save(update_fields=["is_frozen", "freeze_reason", "updated_at"])
        frozen = self.client.post(
            reverse("credit-transfer"),
            {
                "senderUsername": "alice",
                "recipientUsername": "test",
                "amount": "1.00",
                "reason": "Проверка заморозки",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="transfer-frozen-wallet",
            **self.auth,
        )
        summary = self.client.get(reverse("credit-summary"), **self.auth)
        self.assertEqual(frozen.status_code, 423)
        self.assertTrue(summary.json()["account"]["isFrozen"])
        self.assertTrue(summary.json()["capabilities"]["transfersEnabled"])

    def test_staff_freezes_own_wallet_without_username_and_is_audited(self):
        payload = {
            "action": "freeze",
            "reason": "Security review",
        }
        created = self.client.post(
            reverse("credit-admin-operation"),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="admin-freeze-self-001",
            **self.auth,
        )
        replay = self.client.post(
            reverse("credit-admin-operation"),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="admin-freeze-self-001",
            **self.auth,
        )
        regular = User.objects.create_user(username="regular", password="password")
        regular_auth = {
            "HTTP_X_USER_TOKEN": UserKey.objects.create(user=regular).key,
        }
        forbidden = self.client.post(
            reverse("credit-admin-operation"),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="admin-freeze-self-002",
            **regular_auth,
        )
        retired_adjustment = self.client.post(
            reverse("credit-admin-operation"),
            {
                "action": "adjustment",
                "amount": "25.50",
                "reason": "Old action",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="admin-adjustment-retired",
            **self.auth,
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(retired_adjustment.status_code, 400)
        account = CreditAccount.objects.get(user=self.user)
        self.assertTrue(account.is_frozen)
        self.assertEqual(account.freeze_reason, "Security review")
        self.assertEqual(CreditAdminAuditEvent.objects.count(), 1)

        unfrozen = self.client.post(
            reverse("credit-admin-operation"),
            {
                "action": "unfreeze",
                "reason": "Review complete",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="admin-unfreeze-self-001",
            **self.auth,
        )
        audit = self.client.get(
            reverse("credit-admin-audit"),
            {"username": self.user.username},
            **self.auth,
        )
        self.assertEqual(unfrozen.status_code, 201)
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(len(audit.json()["items"]), 2)

    @override_settings(CREDITS_DEMO_TOP_UP_ENABLED=True)
    def test_transfer_is_staff_only_and_requires_a_reason(self):
        self._top_up()
        regular = User.objects.create_user(username="regular", password="password")
        regular_auth = {
            "HTTP_X_USER_TOKEN": UserKey.objects.create(user=regular).key,
        }
        payload = {
            "senderUsername": "alice",
            "recipientUsername": "test",
            "amount": "1.00",
            "reason": "Support transfer",
        }

        forbidden = self.client.post(
            reverse("credit-transfer"),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="admin-transfer-forbidden",
            **regular_auth,
        )
        missing_reason = self.client.post(
            reverse("credit-transfer"),
            {key: value for key, value in payload.items() if key != "reason"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="admin-transfer-no-reason",
            **self.auth,
        )

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(missing_reason.status_code, 400)

    def test_staff_can_transfer_between_other_users_with_audited_actor(self):
        sender = User.objects.create_user(username="producer", password="password")
        CreditAccount.objects.create(
            user=sender,
            available_balance=Decimal("20.00"),
        )
        payload = {
            "senderUsername": sender.username,
            "recipientUsername": self.recipient.username,
            "amount": "7.50",
            "reason": "Project allocation",
        }

        response = self.client.post(
            reverse("credit-transfer"),
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="admin-transfer-others-001",
            **self.auth,
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["transfer"]["sender"], sender.username)
        self.assertEqual(response.json()["auditEvent"]["actor"], self.user.username)
        self.assertEqual(
            CreditAccount.objects.get(user=sender).available_balance,
            Decimal("12.50"),
        )
        self.assertEqual(
            CreditAccount.objects.get(user=self.recipient).available_balance,
            Decimal("7.50"),
        )

        recipient_account = CreditAccount.objects.get(user=self.recipient)
        recipient_account.is_frozen = True
        recipient_account.save(update_fields=["is_frozen", "updated_at"])
        blocked = self.client.post(
            reverse("credit-transfer"),
            {**payload, "amount": "1.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="admin-transfer-frozen-recipient",
            **self.auth,
        )
        self.assertEqual(blocked.status_code, 409)

    def test_generation_spending_statistics_group_project_domain_and_period(self):
        project = Project.objects.create(
            owner=self.user,
            title="Film",
            format="other",
            annotation="",
            synopsis="",
        )
        CreditAccount.objects.create(
            user=self.user,
            available_balance=Decimal("1.00"),
        )
        reserve_generation(
            user=self.user,
            domain="poster",
            job_id="stats-poster-1",
            provider="test",
            model_name="test-model",
            estimated_cost=Decimal("0.25"),
            reservation_amount=Decimal("0.25"),
            project=project,
            operation="generate",
        )

        class Provider:
            def usage_snapshot(self):
                return {"costUsd": "0.20", "costSource": "provider"}

        capture_provider_generation(
            domain="poster",
            job_id="stats-poster-1",
            provider=Provider(),
        )
        response = self.client.get(
            reverse("credit-spending-statistics"),
            {"periodDays": 30},
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["totalCharged"], "0.20")
        self.assertEqual(response.json()["byDomain"][0]["domain"], "poster")
        self.assertEqual(response.json()["byProject"][0]["projectTitle"], "Film")

    def test_project_owner_can_set_clear_and_read_generation_budget(self):
        project = Project.objects.create(
            owner=self.user,
            title="Budget Film",
            format="other",
            annotation="",
            synopsis="",
        )

        initial = self.client.get(reverse("credit-project-budget-list"), **self.auth)
        updated = self.client.patch(
            reverse("credit-project-budget-detail", args=[project.pk]),
            {"limit": "12.50"},
            format="json",
            **self.auth,
        )
        cleared = self.client.patch(
            reverse("credit-project-budget-detail", args=[project.pk]),
            {"limit": None},
            format="json",
            **self.auth,
        )

        self.assertEqual(initial.status_code, 200)
        self.assertIsNone(initial.json()["items"][0]["limit"])
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["limit"], "12.50")
        self.assertEqual(updated.json()["remaining"], "12.50")
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.json()["limit"])

    def test_non_owner_cannot_change_project_budget(self):
        project = Project.objects.create(
            owner=self.recipient,
            title="Foreign Film",
            format="other",
            annotation="",
            synopsis="",
        )

        response = self.client.patch(
            reverse("credit-project-budget-detail", args=[project.pk]),
            {"limit": "10.00"},
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 403)


class GenerationCreditSettlementTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="billing", password="password")
        self.account = CreditAccount.objects.create(
            user=self.user,
            available_balance=Decimal("1.000000"),
        )

    def _reserve(self, job_id: str = "job-1"):
        return reserve_generation(
            user=self.user,
            domain="character",
            job_id=job_id,
            provider="litellm",
            model_name="gemini/gemini-2.5-flash-image",
            estimated_cost=Decimal("0.050000"),
            reservation_amount=Decimal("0.050000"),
            pricing_snapshot={"source": "google"},
        )

    def test_reserve_capture_release_remainder_and_replay_are_idempotent(self):
        first = self._reserve()
        replay = self._reserve()

        class Provider:
            def usage_snapshot(self):
                return {"costUsd": "0.039000", "costSource": "provider"}

        settled = capture_provider_generation(
            domain="character",
            job_id="job-1",
            provider=Provider(),
        )
        settled_replay = capture_provider_generation(
            domain="character",
            job_id="job-1",
            provider=Provider(),
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertFalse(settled.replayed)
        self.assertTrue(settled_replay.replayed)
        charge = GenerationCharge.objects.get(pk=first.charge.pk)
        self.assertEqual(charge.status, GenerationChargeStatus.CAPTURED)
        self.assertEqual(charge.actual_cost, Decimal("0.039000"))
        self.assertFalse(charge.cost_is_estimate)
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance, Decimal("0.961000"))
        self.assertEqual(self.account.reserved_balance, Decimal("0.000000"))
        self.assertEqual(
            list(
                self.account.ledger_entries.order_by("created_at").values_list(
                    "operation_type", flat=True
                )
            ),
            ["reserve", "capture", "release"],
        )

    def test_failure_releases_full_reservation_once(self):
        self._reserve("job-failed")

        released = release_generation(
            domain="character",
            job_id="job-failed",
            reason="provider_failed",
        )
        replay = release_generation(
            domain="character",
            job_id="job-failed",
            reason="provider_failed",
        )

        self.assertFalse(released.replayed)
        self.assertTrue(replay.replayed)
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance, Decimal("1.000000"))
        self.assertEqual(self.account.reserved_balance, Decimal("0.000000"))

    def test_reservation_rejects_insufficient_balance_without_charge(self):
        with self.assertRaises(InsufficientCredits):
            reserve_generation(
                user=self.user,
                domain="poster",
                job_id="expensive-job",
                provider="openrouter-images",
                model_name="google/gemini-3.1-flash-image-preview",
                estimated_cost=Decimal("2.000000"),
                reservation_amount=Decimal("2.000000"),
            )

        self.assertFalse(GenerationCharge.objects.exists())
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance, Decimal("1.000000"))

    def test_automatic_route_never_charges_above_confirmed_reservation(self):
        reserve_generation(
            user=self.user,
            domain="character",
            job_id="bounded-route",
            provider="gemini-native",
            model_name="gemini-2.5-flash-image",
            estimated_cost=Decimal("0.030000"),
            reservation_amount=Decimal("0.050000"),
            routing_mode="economy",
        )

        settled = capture_generation(
            domain="character",
            job_id="bounded-route",
            actual_cost=Decimal("0.080000"),
        )

        self.assertEqual(settled.charge.actual_cost, Decimal("0.080000"))
        self.assertEqual(settled.charge.charged_amount, Decimal("0.050000"))
        self.assertEqual(settled.charge.uncovered_cost, Decimal("0.030000"))
        self.account.refresh_from_db()
        self.assertEqual(self.account.available_balance, Decimal("0.950000"))
        self.assertEqual(self.account.reserved_balance, Decimal("0.000000"))

    def test_project_budget_counts_captured_and_reserved_generation_costs(self):
        collaborator = User.objects.create_user(
            username="project-member",
            password="password",
        )
        CreditAccount.objects.create(
            user=collaborator,
            available_balance=Decimal("1.000000"),
        )
        project = Project.objects.create(
            owner=self.user,
            title="Limited Film",
            format="other",
            annotation="",
            synopsis="",
            credit_budget_limit=Decimal("0.080000"),
        )
        reserve_generation(
            user=self.user,
            domain="character",
            job_id="budget-job-1",
            provider="test",
            model_name="test-model",
            estimated_cost=Decimal("0.050000"),
            reservation_amount=Decimal("0.050000"),
            project=project,
        )

        with self.assertRaises(ProjectCreditBudgetExceeded):
            reserve_generation(
                user=collaborator,
                domain="poster",
                job_id="budget-job-2",
                provider="test",
                model_name="test-model",
                estimated_cost=Decimal("0.040000"),
                reservation_amount=Decimal("0.040000"),
                project=project,
            )

        release_generation(
            domain="character",
            job_id="budget-job-1",
            reason="provider_failed",
        )
        accepted = reserve_generation(
            user=collaborator,
            domain="poster",
            job_id="budget-job-2",
            provider="test",
            model_name="test-model",
            estimated_cost=Decimal("0.040000"),
            reservation_amount=Decimal("0.040000"),
            project=project,
        )

        self.assertFalse(accepted.replayed)


class CreditTransferConcurrencyTest(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.sender = User.objects.create_user(username="sender", password="password")
        self.first = User.objects.create_user(username="first", password="password")
        self.second = User.objects.create_user(username="second", password="password")
        CreditAccount.objects.create(
            user=self.sender,
            available_balance=Decimal("100.00"),
        )

    def _transfer(self, username: str, key: str) -> str:
        close_old_connections()
        try:
            sender = User.objects.get(pk=self.sender.pk)
            transfer_credits(
                sender=sender,
                recipient_username=username,
                amount=Decimal("80.00"),
                note="",
                idempotency_key=key,
            )
            return "success"
        except InsufficientCredits:
            return "insufficient"
        finally:
            close_old_connections()

    def test_parallel_transfers_cannot_overdraw_account(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda args: self._transfer(*args),
                    [
                        ("first", "parallel-transfer-first"),
                        ("second", "parallel-transfer-second"),
                    ],
                )
            )

        self.assertCountEqual(results, ["success", "insufficient"])
        self.assertEqual(
            CreditAccount.objects.get(user=self.sender).available_balance,
            Decimal("20.00"),
        )
        recipient_total = sum(
            CreditAccount.objects.filter(user__in=[self.first, self.second])
            .values_list("available_balance", flat=True),
            Decimal("0.00"),
        )
        self.assertEqual(recipient_total, Decimal("80.00"))
