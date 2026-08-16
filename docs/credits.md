# Craft credits and generation routing

Craft credits are an internal, non-withdrawable product balance. The MVP gives
each authenticated user an account, an append-only operation history, a local
demo top-up, staff-administered transfers, project generation budgets,
generation spending statistics, and guarded model routing.
It does not connect to a bank, payment processor, OpenRouter balance, or Gemini
account.

## API

All endpoints require the normal `X-User-Token` access header.

| Endpoint | Purpose |
|---|---|
| `GET /api/credits/summary/` | Available/reserved balance, 30-day totals, and enabled capabilities |
| `GET /api/credits/history/` | Paginated ledger; accepts `limit`, `offset`, and optional `operationType` |
| `POST /api/credits/demo-top-up/` | Add demo credits when the feature flag is enabled |
| `POST /api/credits/transfers/` | Staff-only audited transfer between exact sender and recipient logins |
| `GET /api/credits/project-budgets/` | Budget, captured spend, active reserve, and remainder for projects owned by the current user |
| `PATCH /api/credits/project-budgets/{projectId}/` | Owner-only update or removal of a project's lifetime generation limit |
| `POST /api/credits/generation-estimate/` | Estimate the provider-native cost and check the available balance before enqueue |
| `GET /api/credits/spending-statistics/` | Captured generation costs by domain, project, and day for 7/30/90/365 days |
| `POST /api/credits/admin/operations/` | Staff-only freeze or unfreeze of the current staff user's wallet, with a required reason |
| `GET /api/credits/admin/audit/` | Staff-only immutable audit for one user's wallet |

Demo top-ups, transfers, and wallet-state changes require an `Idempotency-Key`
header. Replaying the same
key and payload returns the original operation without changing balances. Using
the key for different data returns `409 IDEMPOTENCY_KEY_REUSED`.

Administrative operations also require an idempotency key. Amounts are decimal strings with up to six fractional digits, so small
provider charges are not rounded away. Only staff can initiate a transfer and
must provide the sender login, recipient login, amount, and reason. A transfer
can use any available credits, including credits received through the demo
top-up or another transfer. Reserved credits cannot be transferred. Transfers
are not withdrawable. Per-transfer and rolling 24-hour amount/count limits are exposed
by the summary endpoint and configured through `CREDITS_TRANSFER_MAX_AMOUNT`,
`CREDITS_TRANSFER_DAILY_LIMIT`, and `CREDITS_TRANSFER_DAILY_COUNT_LIMIT`.

A staff member can freeze or unfreeze their own wallet from the credits page;
the action does not ask for a target username. Frozen wallets cannot start
generation, top up, send, or receive transfers.
Existing reservations can still settle so money is not stranded. Every manual
freeze, unfreeze, and staff transfer stores the actor, reason, and idempotency
key in append-only `CreditAdminAuditEvent` rows. Historical adjustment/refund
event and ledger values remain readable, but the runtime API no longer creates
manual adjustments or manual refunds; demo top-up is the only manual credit
grant in this MVP.

## Persistence and lifecycle

`CreditAccount` caches the available and reserved balance for fast reads.
`CreditLedgerEntry` is the append-only audit trail and records both balance
deltas and the resulting balance snapshot. A transfer writes linked outgoing
and incoming entries under one correlation ID while both accounts are locked in
one PostgreSQL transaction. `GenerationCharge` is the idempotent settlement
record keyed by generation domain and job ID.

`Project.credit_budget_limit` is an optional lifetime cap shared by every
generation attached to that project. The project owner can set or clear it.
Captured charges plus all active reservations count toward the limit, regardless
of which member's personal wallet paid. The reservation transaction locks the
project row before checking the cap, so concurrent jobs cannot reserve past it.
Releasing a failed or cancelled reservation restores project capacity. Provider
usage can settle above an estimate; in that case the project may show as over
limit and subsequent paid generations remain blocked until the limit is raised.

Before a paid job is queued, Craft estimates the original provider tariff and
moves that amount from available to reserved credits in the same transaction as
job creation. Insufficient balance aborts the enqueue. On success, Craft
captures the provider-reported cost and releases any unused reservation. On
failure or cancellation it releases the full reservation. Replayed lifecycle
events do not charge twice.

For image generation the request can use `manual`, `economy`, `fast`,
`balanced`, or `quality` routing. Manual mode keeps the model chosen by the
user. Automatic modes deterministically rank only configured, compatible model
specifications and persist the decision with the durable job. They can retry
one fallback only for provider configuration, authorization, availability, or
upstream failures; validation and safety failures never switch provider. Craft
reserves the sum of the primary and fallback estimates and never charges an
automatic route above that confirmed bound. The job billing payload records
the selected provider/model and sanitized attempt results without prompts or
generated content.

One Craft credit currently equals one US dollar of provider cost and the Craft
markup is zero. There is no editable Craft price catalog: tariff snapshots live
with provider model definitions, and dynamic OpenRouter models carry the
pricing returned by its model catalog. OpenRouter response `usage.cost` is used
as the actual cost. Native Gemini/LiteLLM paths record sanitized provider usage
and calculate the charge from the provider rate when an exact cost is not
returned. If a provider supplies no usable cost, settlement retains the enqueue
estimate and marks it as estimated. Prompts and generated content are never
stored in billing usage metadata.

Mock image generation, the current local music provider, and local 3D jobs cost
zero. Paid music providers remain blocked until their adapter exposes supported
billing metadata.

## Demo configuration

`CREDITS_DEMO_TOP_UP_ENABLED=true` enables the top-up stub. The default follows
`DJANGO_DEBUG`: enabled in explicit debug environments and disabled otherwise.
Keep it disabled in production. The frontend reads the capability from the
summary endpoint and hides the demo control when unavailable.

`CREDITS_LOW_BALANCE_THRESHOLD` controls the warning returned by the summary
endpoint. It does not block generation; the normal reservation check remains
authoritative.

There is no real payment webhook, cash withdrawal, currency exchange, or
provider-invoice reconciliation. Routing uses maintained static quality/speed
priorities and configured provider availability; it does not benchmark live
latency or let an LLM make billing decisions.

Migration `0059_project_credit_budgets_and_admin_transfer` adds the nullable
project limit and the administrative-transfer audit event type. Existing
projects remain unlimited.
