from __future__ import annotations

import json
import logging
import sys
import uuid
from unittest.mock import patch

from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import resolve

from w_craft_back.character_studio.services.prompt_compiler import (
    CharacterPromptCompiler,
)
from w_craft_back.observability import (
    JsonLogFormatter,
    SafeDjangoRequestFilter,
    log_context,
)


class StructuredLoggingTests(SimpleTestCase):
    def setUp(self):
        self.formatter = JsonLogFormatter()

    @staticmethod
    def _record(message: str, *, exc_info=None) -> logging.LogRecord:
        return logging.LogRecord(
            name="w_craft_back.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=exc_info,
        )

    def test_formatter_includes_job_context_and_ignores_prompt_extra(self):
        record = self._record("generation_started")
        record.prompt = "private prompt must not be serialized"
        record.token = "private token must not be serialized"

        with log_context(job_id="job-42"):
            payload = json.loads(self.formatter.format(record))

        self.assertEqual(payload["job_id"], "job-42")
        self.assertNotIn("prompt", payload)
        self.assertNotIn("token", payload)
        self.assertNotIn("private prompt", json.dumps(payload))

    def test_formatter_omits_exception_message_and_traceback(self):
        try:
            raise ValueError("private prompt in exception")
        except ValueError:
            record = self._record("provider_failed", exc_info=sys.exc_info())

        payload = json.loads(self.formatter.format(record))

        self.assertEqual(payload["exception_type"], "ValueError")
        self.assertNotIn("private prompt", json.dumps(payload))

    def test_django_request_filter_redacts_signed_media_token(self):
        request = RequestFactory().get("/api/media/super-secret-signed-token")
        request.resolver_match = resolve(request.path)
        request.request_id = "request-42"
        record = logging.LogRecord(
            name="django.request",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="Not Found: %s",
            args=(request.path,),
            exc_info=None,
        )
        record.request = request

        SafeDjangoRequestFilter().filter(record)
        payload = json.loads(self.formatter.format(record))

        self.assertEqual(payload["message"], "django_request_error")
        self.assertEqual(payload["request_id"], "request-42")
        self.assertEqual(payload["route"], "/api/media/<path:token>")
        self.assertNotIn("super-secret-signed-token", json.dumps(payload))

    def test_django_request_filter_does_not_label_success_as_error(self):
        request = RequestFactory().get("/health/live")
        request.resolver_match = resolve(request.path)
        record = logging.LogRecord(
            name="django.server",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='"GET %s HTTP/1.1" 200 12',
            args=(request.path,),
            exc_info=None,
        )
        record.request = request
        record.status_code = 200

        SafeDjangoRequestFilter().filter(record)
        payload = json.loads(self.formatter.format(record))

        self.assertEqual(payload["message"], "django_request_completed")
        self.assertEqual(payload["status_code"], 200)

    def test_prompt_compiler_logs_metadata_but_not_prompt_text(self):
        secret_prompt = "private character backstory"
        compiler = CharacterPromptCompiler()

        with patch(
            "w_craft_back.character_studio.services.prompt_compiler.logger.info"
        ) as log_info:
            compiled = compiler.compile(
                character=None,
                appearance=None,
                outfit=None,
                text_refinement=secret_prompt,
            )

        self.assertIn(secret_prompt, compiled["positive_prompt"])
        self.assertNotIn(secret_prompt, repr(log_info.call_args))
        self.assertEqual(log_info.call_args.args, ("character_prompt_compiled",))
        self.assertGreater(log_info.call_args.kwargs["extra"]["prompt_len"], 0)


class RequestCorrelationTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    def test_valid_request_id_is_returned(self):
        response = self.client.get(
            "/health/live",
            HTTP_X_REQUEST_ID="pilot-request-42",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Request-ID"], "pilot-request-42")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_invalid_request_id_is_replaced(self):
        response = self.client.get(
            "/health/live",
            HTTP_X_REQUEST_ID="contains whitespace",
        )

        self.assertEqual(response.status_code, 200)
        uuid.UUID(response["X-Request-ID"])


@override_settings(READINESS_REQUIRE_MODEL3D_WORKER=False)
class HealthProbeTests(TestCase):
    def test_liveness_does_not_run_readiness_checks(self):
        with patch(
            "w_craft_back.health._database_check",
            side_effect=AssertionError("must not run"),
        ):
            response = self.client.get("/health/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readiness_checks_database_storage_and_job_tables(self):
        response = self.client.get("/health/ready")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["components"]["database"]["status"], "ok")
        self.assertEqual(payload["components"]["storage"]["status"], "ok")
        self.assertEqual(
            payload["components"]["generation_jobs"],
            {"status": "ok", "worker_mode": "in_process"},
        )
        self.assertEqual(
            payload["components"]["model3d_worker"],
            {"status": "skipped", "required": False},
        )

    def test_readiness_returns_safe_503_when_storage_is_unavailable(self):
        with patch(
            "w_craft_back.health.default_storage.exists",
            side_effect=RuntimeError("private storage credential"),
        ):
            response = self.client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["components"]["storage"],
            {"status": "failed", "reason": "unavailable"},
        )
        self.assertNotIn("private storage credential", response.content.decode())

    def test_readiness_returns_safe_503_when_database_is_unavailable(self):
        with patch(
            "w_craft_back.health.connection.cursor",
            side_effect=RuntimeError("private database credential"),
        ):
            response = self.client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["components"]["database"],
            {"status": "failed", "reason": "unavailable"},
        )
        self.assertNotIn("private database credential", response.content.decode())

    @override_settings(READINESS_REQUIRE_MODEL3D_WORKER=True)
    def test_readiness_requires_configured_model3d_worker(self):
        with patch("w_craft_back.health._executable_exists", return_value=False):
            response = self.client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["components"]["model3d_worker"],
            {
                "status": "failed",
                "reason": "runtime_unavailable",
                "worker_mode": "detached_process",
            },
        )
