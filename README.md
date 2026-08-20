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

Supported queue names are `character`, `poster`, `music`, and `reference`.
Character 3D reconstruction is dispatched through the `character` queue but
invokes a separate Python/Conda runtime.

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
- `/api/projects/` — project aggregate, team, poster, music, references.
- `/api/projects/{id}/characters...` — Character Studio.
- `/api/credits/` — internal balance, ledger, demo top-up, and transfers.
- `/api/media/{signed-token}` — authorized private media delivery.

Root URL composition is in `backend/urls.py` (the inner Django project directory
is also named `backend`).

## Auth

Auth uses distinct opaque access/refresh tokens. `UserKey` stores SHA-256
digests, expiry, revocation, and rotation state; plaintext tokens are returned
only when issued. Clients send the access token in `X-User-Token`. Refresh is
single-use and atomically rotates both credentials. This is not JWT.

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

## API contract

`docs/openapi.json` is served directly by Django and validated by
`scripts/check_openapi_contract.py`. A contract change also requires updating
the frontend copy at `../who_craft/openapi/w_craft.openapi.json` and regenerating
the frontend client.
