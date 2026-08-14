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
