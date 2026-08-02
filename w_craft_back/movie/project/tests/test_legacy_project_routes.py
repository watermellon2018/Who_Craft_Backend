from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import SimpleTestCase
from rest_framework.test import APIClient


class LegacyProjectRouteRemovalTests(SimpleTestCase):
    legacy_routes = (
        ("post", "/api/projects/create/"),
        ("get", "/api/projects/get-list-projects/"),
        ("delete", "/api/projects/delete-project-by-id/"),
        ("get", "/api/projects/select-project-by-id/"),
        ("post", "/api/projects/update-project-by-id/"),
    )

    def setUp(self) -> None:
        self.client = APIClient()

    def test_legacy_project_routes_are_unmounted(self) -> None:
        for method, path in self.legacy_routes:
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(
                    path,
                    {"payload_secret": "must-not-be-logged"},
                    format="json",
                )
                self.assertEqual(response.status_code, 404)

    def test_unused_project_generation_route_is_unmounted(self) -> None:
        response = self.client.post(
            "/api/projects/1/generation-jobs/",
            {"job_type": "scene_image"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)

    def test_removed_route_attempt_is_logged_without_request_data(self) -> None:
        with self.assertLogs("w_craft_back.request", level="INFO") as captured:
            response = self.client.post(
                "/api/projects/create/?token=query-secret",
                {"payload_secret": "body-secret"},
                format="json",
            )

        self.assertEqual(response.status_code, 404)
        usage_records = [
            record
            for record in captured.records
            if record.getMessage() == "legacy_project_route_requested"
        ]
        self.assertEqual(len(usage_records), 1)
        self.assertEqual(usage_records[0].operation, "create")
        self.assertEqual(usage_records[0].method, "POST")
        rendered_logs = "\n".join(captured.output)
        self.assertNotIn("query-secret", rendered_logs)
        self.assertNotIn("body-secret", rendered_logs)


class ProjectGenerationMigrationGuardTests(SimpleTestCase):
    def test_drop_guard_locks_table_and_rejects_existing_rows(self) -> None:
        migration = import_module(
            "w_craft_back.migrations.0048_remove_unused_project_generation_job"
        )
        historical_model = MagicMock()
        historical_model._meta.db_table = "w_craft_back_projectgenerationjob"
        historical_model.objects.using.return_value.count.return_value = 1
        historical_apps = MagicMock()
        historical_apps.get_model.return_value = historical_model
        connection = MagicMock()
        connection.alias = "audit"
        connection.ops.quote_name.return_value = (
            '"w_craft_back_projectgenerationjob"'
        )
        schema_editor = SimpleNamespace(connection=connection)

        with self.assertRaisesRegex(RuntimeError, "1 row"):
            migration.assert_project_generation_jobs_empty(
                historical_apps,
                schema_editor,
            )

        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.assert_called_once_with(
            'LOCK TABLE "w_craft_back_projectgenerationjob" '
            "IN ACCESS EXCLUSIVE MODE"
        )
        historical_model.objects.using.assert_called_once_with("audit")
