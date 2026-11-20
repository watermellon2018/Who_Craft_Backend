# Personal messaging

`/api/messaging/` provides private text conversations between two authenticated
users. The frontend entry is **Messages** at `/profile/messages`. Every active
user can be found by login, public username, or display name, including users
without a profile. Profile privacy and comment settings do not disable this
feature. Groups, attachments, typing/presence, notifications, and email delivery
are outside this version.

Clients authenticate with `X-User-Token`. Every conversation endpoint verifies
participation; unrelated users receive 404. Search and conversation responses
contain only public identity fields and signed avatar URLs, never email or
credentials. The normal authenticated API throttle applies.

## API

| Path relative to `/api/messaging/` | Methods | Purpose |
|---|---|---|
| `users/` | GET | Search `q`, optional `@` prefix; `limit`/`offset` |
| `unread/` | GET | Total visible unread incoming messages |
| `conversations/` | GET, POST | List chats; open pair using `recipient_id` |
| `conversations/{id}/` | GET, DELETE | Fetch state; clear/hide only for the actor |
| `conversations/{id}/messages/` | GET, POST | History; send `text` + UUID `client_id` |
| `conversations/{id}/messages/{messageId}/` | PATCH, DELETE | Edit with `text` + `expected_version`; delete own message |
| `conversations/{id}/changes/` | GET | Poll `after_revision` and optional `limit` |
| `conversations/{id}/read/` | POST | Advance `through_sequence` |

Search requires 2–100 characters after normalization; blank/short queries return
an empty list. Search pages default to 20, conversation pages to 30, history to
50, and changes to 100. All page limits are at most 100. Conversation lists use
the latest new-send time (`updated_at`), with a deterministic ID tie-breaker.
Reads, edits, and deletions do not move a chat to the top.

Text is trimmed, nonblank, and limited to 4,000 Unicode characters. Open-chat
requests return 200; successful sends (including retries) return 201. Other
successful operations return 200. Errors use the shared `{error: {code,
message, ...}}` envelope. See `docs/openapi.json` for complete schemas.

## History, retries, and polling

There is one canonical conversation per ordered user pair. The initiator sees
an empty new conversation; the recipient sees it after the first new send.
Only the author can edit or delete their messages. Editing has no time limit,
sets `edited_at`, and requires the current `version` to prevent lost updates.
Deletion erases the stored text and retains a tombstone with `deleted_at` and
empty text for both participants. Repeated deletion is idempotent.

Personal chat deletion sets `cleared_through` to the latest message sequence
and hides the chat. It neither erases the other participant's history nor
advances read receipts. Old message IDs become inaccessible to the clearing
user. Opening the same pair or editing old messages does not restore it. A
later **new** send reveals the chat again, showing only messages above the
personal clear boundary.

Send retries use `(conversation, sender, client_id)` plus an immutable SHA-256
fingerprint of the normalized original text. Retrying the same original send
returns the current message (including edits or its deletion tombstone).
Different text under the same ID returns `CLIENT_ID_CONFLICT` (409). A replay
below the sender's clear boundary returns `MESSAGE_CLEARED` (409), without
disclosing the hidden text or revealing the conversation.

Messages have an increasing per-conversation `sequence`; all message changes
have distinct `version` values from the conversation's `revision` counter.
History returns each page in ascending sequence order, `next_before`, and a
snapshot `sync_revision`. Polling returns the latest state of changed messages
ordered by version, including edits/deletions outside the latest history page.
While `has_more` is true, use `next_revision` from the last returned message;
otherwise it advances to the locked snapshot's revision. Apply changes by ID
and version and reconcile the accompanying participant/clear state. Do not
replace a polling cursor with a later history-page cursor before reconciling
intervening changes.

Clients send read progress only for displayed messages in an active chat. The
server accepts existing visible sequences, advances monotonically, and rejects
future or newly cleared boundaries. Unread counts exclude own messages,
deleted messages, and cleared history. Edits do not make messages unread again.

## Persistence and account closure

The existing `w_craft_back` Django app registers `MessagingConversation`,
`MessagingParticipant`, and `MessagingMessage` from `w_craft_back/messaging/`.
Apply migration `0068_personal_messaging` during the usual deployment migration
step. No additional service, worker, dependency, or environment setting is
required; PostgreSQL supplies the transaction/row-lock semantics.

Mutations lock the two user rows in ascending ID order before locking the
conversation. Sequences and revisions are allocated while holding that lock.
History/change snapshots also lock the conversation so the message page and
cursor describe the same state. Multi-conversation locks use ascending UUID
order to avoid deadlocks.

The existing account-closure transaction first locks and deactivates its user,
then calls `scrub_user_messages`. That helper locks conversations in UUID order
and never locks peers. It erases all authored text, allocating a separate
tombstone revision per message so paginated polling cannot skip removals. The
surviving participant retains their own messages, sees a blank inactive peer
identity, and cannot send new messages. The protected user foreign keys remain
valid because closure anonymizes rather than physically deletes accounts.

## Checks

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test w_craft_back.messaging.tests w_craft_back.profile.tests.test_account_deletion
python -m flake8 --jobs=1 w_craft_back/messaging
python scripts/check_openapi_contract.py
```

The messaging tests include real PostgreSQL transaction races for opposite
pair creation, duplicate sends, concurrent edits, clear/send ordering, and
account closure. Those race tests are skipped on other database engines.
