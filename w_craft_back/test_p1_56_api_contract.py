"""Regression tests for the backend/frontend API contract."""

from __future__ import annotations

from django.test import Client, SimpleTestCase

from w_craft_back.api_contract import load_openapi_schema
from w_craft_back.api_errors import normalize_error_payload
from w_craft_back.movie.poster.serializers import (
    PROMPT_MAX_LENGTH,
    PosterGenerateSerializer,
)
from w_craft_back.movie.project.project_images import MAX_PROJECT_IMAGE_BYTES
from w_craft_back.movie.project.serializers import (
    PROJECT_ANNOTATION_MAX_LENGTH,
    PROJECT_SYNOPSIS_MAX_LENGTH,
    ProjectCreateSerializer,
    ProjectUpdateSerializer,
)


class ApiContractTests(SimpleTestCase):
    def setUp(self) -> None:
        load_openapi_schema.cache_clear()
        self.schema = load_openapi_schema()

    def test_public_schema_endpoint_serves_checked_in_document(self) -> None:
        response = Client().get("/api/schema/openapi.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["openapi"], "3.0.3")

    def test_header_auth_and_tree_create_contract(self) -> None:
        security = self.schema["components"]["securitySchemes"]["userToken"]
        create_schema = self.schema["components"]["schemas"][
            "CharacterTreeCreateRequest"
        ]

        self.assertEqual(security["in"], "header")
        self.assertEqual(security["name"], "X-User-Token")
        self.assertNotIn("token_user", create_schema["properties"])
        self.assertNotIn("projectId", create_schema["properties"])
        self.assertEqual(
            set(create_schema["properties"]["type"]["enum"]),
            {"folder", "character"},
        )

    def test_limits_match_runtime_serializers(self) -> None:
        constraints = self.schema["x-contract-constraints"]
        schemas = self.schema["components"]["schemas"]

        self.assertEqual(constraints["posterPromptMaxLength"], PROMPT_MAX_LENGTH)
        self.assertEqual(
            constraints["projectPosterMaxBytes"],
            MAX_PROJECT_IMAGE_BYTES,
        )
        self.assertEqual(
            schemas["PosterGenerateRequest"]["properties"]["prompt"]["maxLength"],
            PosterGenerateSerializer().fields["prompt"].max_length,
        )
        self.assertEqual(
            constraints["projectAnnotationMaxLength"],
            PROJECT_ANNOTATION_MAX_LENGTH,
        )
        self.assertEqual(
            constraints["projectSynopsisMaxLength"],
            PROJECT_SYNOPSIS_MAX_LENGTH,
        )
        for serializer_class in (ProjectCreateSerializer, ProjectUpdateSerializer):
            serializer = serializer_class()
            self.assertEqual(
                serializer.fields["annotation"].max_length,
                PROJECT_ANNOTATION_MAX_LENGTH,
            )
            self.assertEqual(
                serializer.fields["synopsis"].max_length,
                PROJECT_SYNOPSIS_MAX_LENGTH,
            )

    def test_error_payload_is_normalized_without_dropping_field_errors(self) -> None:
        payload = normalize_error_payload(
            {
                "detail": "validation error",
                "errors": {
                    "annotation": [
                        "Ensure this field has no more than 800 characters."
                    ]
                },
            },
            400,
        )

        self.assertEqual(payload["error"]["code"], "BAD_REQUEST")
        self.assertIn("800", payload["error"]["message"])
        self.assertEqual(payload["error"]["fields"], payload["errors"])
        self.assertEqual(payload["detail"], "validation error")

    def test_api_middleware_wraps_real_drf_errors(self) -> None:
        response = Client().get("/api/projects/1/character-tree/")

        self.assertIn(response.status_code, {401, 403})
        payload = response.json()
        self.assertIsInstance(payload["error"]["code"], str)
        self.assertIsInstance(payload["error"]["message"], str)
        self.assertEqual(payload["detail"], payload["error"]["message"])
