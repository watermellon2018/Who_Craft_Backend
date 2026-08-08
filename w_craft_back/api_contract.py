"""Serve the checked-in OpenAPI contract used to generate frontend types."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@lru_cache(maxsize=1)
def load_openapi_schema() -> dict[str, Any]:
    """Load the canonical schema once per application process."""

    schema_path = Path(settings.BASE_DIR) / "docs" / "openapi.json"
    with schema_path.open("r", encoding="utf-8") as schema_file:
        return json.load(schema_file)


@require_GET
def openapi_schema_view(request) -> JsonResponse:
    """Return the public OpenAPI document without requiring authentication."""

    return JsonResponse(load_openapi_schema())
