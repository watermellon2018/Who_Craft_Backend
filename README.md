# Craft backend

Django 5.2 + Django REST Framework backend for Craft. It owns authentication,
PostgreSQL persistence, project/team workflows, Character Studio, posters,
Music Studio, Reference Library, profiles/subscriptions, private media, and
durable generation workers.

The sibling frontend is `../who_craft/`. Cross-system documentation is in
`../README.md`, `../AGENTS.md`, and `../docs/`.

## Local setup

Recommended runtime: Python 3.11 and PostgreSQL.

```bash
cp .env.example .env
# Set DJANGO_SECRET_KEY and W_CRAFT_POSTGRES_USER/PASSWORD.
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Start generation in a separate process:

```bash
python manage.py run_generation_worker --queue all
```

Supported queue names are `character`, `poster`, `music`, `sound_effect`, and
`reference`.
Character 3D reconstruction is dispatched through the `character` queue but
invokes a separate Python/Conda runtime.

Music generation jobs remain queued until a generation worker polls the
`music` queue. Music Studio shows a delayed-queue warning when a job has not
been claimed within 30 seconds; start `run_generation_worker --queue music`
before treating provider polling as active generation.

## Checks

```bash
python manage.py check
python manage.py test
flake8
python scripts/check_openapi_contract.py
```

Use a focused Django test label while iterating, then the full suite for broad
changes. Tests and most Django commands require valid `.env` settings and a
reachable PostgreSQL database.

Every new user-visible or operational feature must include a short reference in
the same change. Document its purpose, API or entry point, required configuration
and permissions, main lifecycle, and important limitations. Update the relevant
parent-level `../docs/` page rather than leaving the implementation or a dated
task plan as the only documentation. Purely visual styling/layout work does not
require documentation when backend behavior, APIs, configuration, data, and
operations are unchanged.

## Runtime endpoints

- `GET /health/live` — process liveness only.
- `GET /health/ready` — database, storage, character/poster job tables, and the
  optional 3D runtime. Music/reference tables are not currently probed.
- `GET /api/schema/openapi.json` — checked-in OpenAPI 3.0 document.
- `/api/auth/` — register/login/refresh/logout.
- `GET/PATCH /api/profile/me/` — current profile; `DELETE` closes and
  anonymizes the current account after password confirmation.
- `/api/projects/` — project aggregate, team, poster, music, references.
- `/api/projects/{id}/dashboard/` — dashboard and dynamically calculated
  [project readiness](docs/project-readiness.md).
- `/api/projects/{id}/characters...` — Character Studio.
- `GET /api/projects/{id}/scenes/missing-characters/` — significant screenplay
  names that do not yet have a visible Character Studio character.
- `GET /api/projects/{id}/video/preparation/` — the current video prerequisites:
  missing characters, empty scenes, and incomplete or stale storyboards.
- `/api/credits/` — internal balance, ledger, demo top-up, and transfers.
- `/api/media/{signed-token}` — authorized private media delivery.

Root URL composition is in `backend/urls.py` (the inner Django project directory
is also named `backend`).

## Auth

Auth uses distinct opaque access/refresh tokens. `UserKey` stores SHA-256
digests, expiry, revocation, and rotation state; plaintext tokens are returned
only when issued. Clients send the access token in `X-User-Token`. Refresh is
single-use and atomically rotates both credentials. This is not JWT.

### Account closure

`DELETE /api/profile/me/` accepts JSON `{"current_password":"..."}` and is
limited to five attempts per authenticated user per hour. The throttle uses
the configured Django cache; the default local-memory cache is per process, so
a shared cache is required for a global limit across multiple web processes.
A missing password
returns `ACCOUNT_DELETE_PASSWORD_REQUIRED`; an invalid password returns
`ACCOUNT_DELETE_PASSWORD_INVALID`. Closure is blocked with
`ACCOUNT_HAS_OWNED_PROJECTS` and `ownedProjectCount` until every owned project
is transferred or deleted. Projects are never deleted automatically.

On success, profile media/metadata, interests, social links, subscriptions,
non-owner project memberships, incoming invitations, and authentication tokens
are removed. The Django user is deactivated and anonymized rather than deleted,
so protected generation attribution and the credit ledger remain referentially
valid. The response is `204`, and the previous access/refresh credentials stop
working immediately. Historical project/activity records may retain author,
title, or metadata snapshots created before closure; audit history is not
rewritten. Physical media deletion runs after the database commit through the
storage cleanup hook and is logged for operational retry if storage is
temporarily unavailable.

## Data, jobs, and media

- PostgreSQL is required and also coordinates durable jobs/leases. There is no
  Redis/Celery dependency.
- Web and workers must share PostgreSQL and private media storage.
- Database rows store media metadata/storage keys, never image/audio binaries.
- Reference and music versions are immutable; logical entities point to the
  currently active version.
- Character visual changes preserve `CharacterRevision` history.

### Upgrade through migration 0054

Migration `0054_normalize_runtime_values` makes the current contracts
canonical-only. It normalizes retired project-format aliases, imported
Music/Reference provenance values, pending music verification state, and old
Character Studio hair-length values. The migration aborts before changing data
when it encounters an unsupported stored value; correct that value and rerun
the migration instead of bypassing the guard.

The normalization is intentionally forward-only: reversing the schema migration
does not recreate retired aliases because several aliases collapse into one
canonical value. Take a database backup before deployment when rollback must
restore the exact pre-upgrade values.

After upgrading, use `python manage.py verify_pending_music_assets` to validate
music files whose metadata is still pending. The removed metadata-only music
POST is no longer part of the API; tracks enter the library through the current
upload or generation flows.

## Configuration

`backend/.env.example` is the maintained environment-variable inventory. The
application refuses to start without `DJANGO_SECRET_KEY` and PostgreSQL user/
password. Mock providers are suitable for local Character/Reference generation;
review provider-specific defaults before making external calls.

Music Studio has pluggable provider modes:

- `MUSIC_GENERATION_PROVIDER=mock` produces deterministic local WAV files and
  is the safe development default. Set `MUSIC_ALLOW_MOCK=false` outside local
  development so users cannot select the zero-cost mock route.
- `MUSIC_GENERATION_PROVIDER=stability` sends paid instrumental requests to
  Stability AI Stable Audio 3.0. Set `STABILITY_API_KEY` in both the web and
  worker environments. The adapter submits asynchronously, stores only the
  provider generation ID, polls from the durable music queue, validates the
  returned MP3/WAV, and moves it into private media storage.
- Google Lyria 3 models can use either the direct Google Interactions API with
  `GEMINI_API_KEY` or OpenRouter with `OPENROUTER_API_KEY`. Direct Google is the
  cheaper preferred route. OpenRouter passes through the same `$0.08`/`$0.04`
  inference tariff but its 5.5% pay-as-you-go credit-purchase fee makes the
  effective funding cost about `$0.0844`/`$0.0422`. The resolver chooses the direct
  route when both credentials are configured and uses OpenRouter only when the
  direct route is unavailable before submission. Adapters never retry a
  possibly paid request through the other route after a timeout or malformed
  response.
- `elevenlabs-music-v2` calls ElevenLabs Music v2 directly. It supports one
  instrumental or structured-lyrics result, uses the current five-minute Craft
  product limit, and reserves the configured per-minute price. Configure
  `ELEVENLABS_API_KEY`; no account secret is required at build time.
- `minimax-music-3` is prepared for grandfathered MiniMax accounts only. It is
  unavailable unless both `MINIMAX_API_KEY` and
  `MUSIC_MINIMAX_LEGACY_PAID_ACCESS_CONFIRMED=true` are present. New MiniMax
  accounts cannot currently enable the Music API, and Craft never substitutes
  a free or unofficial model. MiniMax has no dedicated duration parameter, so
  Craft sends the selected duration as an approximate prompt instruction; the
  provider controls the final duration.

Set `MUSIC_DEFAULT_AUDIO_MODEL` to the catalog key used when the client does not
choose a model. `MUSIC_GEMINI_API_BASE_URL` and
`MUSIC_OPENROUTER_API_BASE_URL` must retain their official HTTPS origins in
production. Optional `OPENROUTER_HTTP_REFERER` and `OPENROUTER_APP_TITLE`
values provide OpenRouter attribution. Lyria 3 Pro and Clip are preview models;
verify availability, regional terms, and current provider prices before each
deployment. OpenRouter's generic audio guide and Lyria-specific model page do
not currently publish one fully consistent request schema, so run a
credentialed audio smoke test before enabling that route. Keep
`MUSIC_JOB_LEASE_SECONDS` at least `300`, above the 180-second
synchronous Lyria request timeout with enough margin to stop another worker
from claiming the same paid job while the first request is still in flight. The
effective setting is also clamped to the larger Lyria timeout plus 120 seconds.
`MUSIC_GEMINI_RESPONSE_DEADLINE_SECONDS` and
`MUSIC_OPENROUTER_RESPONSE_DEADLINE_SECONDS` impose a bounded wall-clock stream
deadline (300 seconds by default, capped at 900 seconds).

The Stability adapter currently exposes one instrumental variant per request,
durations up to the application limit (300 seconds by default), and no remote
cancellation, structured lyrics, or audio-reference generation. A lost response
to the initial paid submission is recorded as an unknown outcome and is not
blindly retried. Polling stops after `MUSIC_STABILITY_MAX_POLL_SECONDS` (30
minutes by default). Unknown outcomes conservatively capture the reserved
estimate for provider-billing reconciliation; a confirmed provider result is
also charged if local validation or storage later fails.
`MUSIC_STABILITY_COST_USD_PER_VARIANT` defaults to the current provider price of
`0.26`; verify it against the Stability pricing page during deployment so Craft
never enqueues an unpriced call. The compiled musical brief and bounded scene
summary are sent to Stability AI; raw scripts and API keys are not written to
provider or billing metadata.

Sound Effects is a separate project domain at
`/api/projects/{projectId}/sound-effects/`; generated effects are never stored as
`MusicTrack` rows. ElevenLabs Sound Effects v2 accepts a text description up to
450 characters, optional 0.5-30 second duration, loop mode and prompt influence.
Its durable jobs run on the `sound_effect` queue and store verified MP3 files in
private project media. Explicit duration uses the configured `$0.12/minute`
rate. Auto duration is disabled until
`SOUND_EFFECTS_ELEVENLABS_AUTO_COST_USD` is set to an account-verified estimate,
because ElevenLabs publishes plan credits for Auto without a public USD
conversion. Before production use, disable third-party SFX sublicensing in the
ElevenLabs account and perform a credentialed smoke test.

The credits wallet and generation settlement flow are described in
[`docs/credits.md`](docs/credits.md). Paid generation first reserves the
provider-native estimate, captures the final provider cost on success, and
releases the reservation on confirmed pre-provider failure or cancellation. Set
`CREDITS_DEMO_TOP_UP_ENABLED=true` only for local/demo use; it grants internal
credits without a payment provider. The same reference documents automatic
model routing, low-balance warnings, owner-managed project budgets,
staff-administered transfers, self-freeze/audit operations, and their
`CREDITS_*` settings.

The optional CUDA/Hunyuan stack must be installed separately:

```bash
conda run -n basic python -m pip install -r requirements-3d.txt
```

Set `READINESS_REQUIRE_MODEL3D_WORKER=false` only when local readiness should not
require that optional runtime.

Never commit `.env`, provider credentials, raw auth tokens, private media,
licensed models/checkpoints, or local database dumps.

## Profile settings, notifications, and comments

Authenticated profile preferences are read and partially updated through
`GET/PATCH /api/profile/settings/`. Interface language and content language are
independent. In-app/email delivery channels and the video comment audience are
server-side profile settings; the existing private-account setting is retained.

Notification producers call the centralized dispatcher in
`w_craft_back.notifications.services`. It checks preferences once, stores an
in-app notification, and/or creates a durable email-delivery row in the same
transaction. The first SMTP attempt runs after commit; failures remain eligible
for `python manage.py retry_notification_emails --limit 100`. Username project
invitations and new shot comments use this dispatcher. Configure Django's email
backend, sender, and timeout before enabling email notifications, and schedule
the bounded retry command operationally. No websocket, SSE,
or polling transport is introduced here: `/api/notifications/` is the durable
notification center, and a future push adapter can publish the same rows.

Shot comments are available at
`/api/projects/{projectId}/video-shots/{shotId}/comments/`. A caller must first
have project access. The video's temporary ownership contract is the owning
project's `owner`; `everyone`, active `followers`, or `nobody` is enforced again
inside the transactional create service. Changing the preference never deletes
existing comments.

`POST /api/auth/logout-all/` revokes the user's current one-to-one opaque
access/refresh credential pair. Because login and refresh rotate that same pair,
this invalidates credentials held by all devices, including the caller.

## API contract

`docs/openapi.json` is served directly by Django and validated by
`scripts/check_openapi_contract.py`. A contract change also requires updating
the frontend copy at `../who_craft/openapi/w_craft.openapi.json` and regenerating
the frontend client.
