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

Both POST operations require an `Idempotency-Key` header. Replaying the same
key and payload returns the original operation without changing balances. Using
the key for different data returns `409 IDEMPOTENCY_KEY_REUSED`.

Amounts are decimal strings with two fractional digits. A transfer can use any
available credits, including credits received through the demo top-up or another
transfer. Reserved credits cannot be transferred. Transfers are not
withdrawable or reversible through this MVP.

## Persistence and lifecycle

`CreditAccount` caches the available and reserved balance for fast reads.
`CreditLedgerEntry` is the append-only audit trail and records both balance
deltas and the resulting balance snapshot. A transfer writes linked outgoing
and incoming entries under one correlation ID while both accounts are locked in
one PostgreSQL transaction.

The current MVP creates accounts lazily. It does not yet reserve or capture
generation costs; those operation types are present for the later billing
integration.

## Demo configuration

`CREDITS_DEMO_TOP_UP_ENABLED=true` enables the top-up stub. The default follows
`DJANGO_DEBUG`: enabled in explicit debug environments and disabled otherwise.
Keep it disabled in production. The frontend reads the capability from the
summary endpoint and hides the demo control when unavailable.

There is no real payment webhook, bank refund, cash withdrawal, exchange rate,
provider reconciliation, or automated AI-provider routing in this MVP.
