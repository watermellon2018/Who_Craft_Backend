from django.test import SimpleTestCase

from scripts.check_migration_history import (
    EXPECTED_PREDECESSOR,
    check_history,
)


class MigrationHistoryPreflightTests(SimpleTestCase):
    def test_accepts_bootstrapped_existing_database(self) -> None:
        check_history({EXPECTED_PREDECESSOR})

    def test_rejects_missing_history_instead_of_faking(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "do not fake"):
            check_history(set())
