"""Fail deployment preflight when the existing app history is not bootstrapped.

Run this before ``manage.py migrate`` on an existing environment. A brand-new
database should instead run the complete migration chain normally; never use
``--fake`` to bypass this check on an existing database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import django
from django.db import connection
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PREDECESSOR = ("w_craft_back", "0050_reference_library")


def check_history(applied: set[tuple[str, str]]) -> None:
    """Require the last pre-package-marker migration on existing databases."""

    if EXPECTED_PREDECESSOR not in applied:
        raise RuntimeError(
            "w_craft_back migration history is not bootstrapped through "
            "0050_reference_library. Stop rollout and reconcile the existing "
            "database history; do not fake migrations automatically."
        )


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
    sys.path.insert(0, str(ROOT))
    django.setup()
    loader = MigrationLoader(connection)
    try:
        loader.check_consistent_history(connection)
        applied = set(MigrationRecorder(connection).applied_migrations())
        check_history(applied)
    except (InconsistentMigrationHistory, RuntimeError) as exc:
        print(f"Migration history preflight failed: {exc}", file=sys.stderr)
        return 1
    print("Migration history preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
