# Craft credits MVP

Craft credits are an internal, non-withdrawable product balance. The MVP gives
each authenticated user an account, an append-only operation history, a local
demo top-up, transfers to another Craft login, and 30-day movement statistics.
It does not connect to a bank, payment processor, OpenRouter balance, or Gemini
account.

## API

All endpoints require the normal `X-User-Token` access header.

| Endpoint | Purpose |
|---|---|
| `GET /api/credits/summary/` | Available/reserved balance, 30-day totals, and enabled capabilities |
| `GET /api/credits/history/` | Paginated ledger; accepts `limit`, `offset`, and optional `operationType` |
| `POST /api/credits/demo-top-up/` | Add demo credits when the feature flag is enabled |
| `POST /api/credits/transfers/` | Atomically transfer available credits to an exact Django login |
| `POST /api/credits/generation-estimate/` | Estimate the provider-native cost and check the available balance before enqueue |

Both POST operations require an `Idempotency-Key` header. Replaying the same
key and payload returns the original operation without changing balances. Using
the key for different data returns `409 IDEMPOTENCY_KEY_REUSED`.

Amounts are decimal strings with up to six fractional digits, so small
provider charges are not rounded away. A transfer can use any
available credits, including credits received through the demo top-up or another
transfer. Reserved credits cannot be transferred. Transfers are not
withdrawable or reversible through this MVP.

## Persistence and lifecycle

`CreditAccount` caches the available and reserved balance for fast reads.
`CreditLedgerEntry` is the append-only audit trail and records both balance
deltas and the resulting balance snapshot. A transfer writes linked outgoing
and incoming entries under one correlation ID while both accounts are locked in
one PostgreSQL transaction. `GenerationCharge` is the idempotent settlement
record keyed by generation domain and job ID.

Before a paid job is queued, Craft estimates the original provider tariff and
moves that amount from available to reserved credits in the same transaction as
job creation. Insufficient balance aborts the enqueue. On success, Craft
captures the provider-reported cost and releases any unused reservation. On
failure or cancellation it releases the full reservation. Replayed lifecycle
events do not charge twice.

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

There is no real payment webhook, bank refund, cash withdrawal, currency
exchange, provider-invoice reconciliation, or automated AI-provider routing in
this MVP.
