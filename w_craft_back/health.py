"""Cheap liveness and dependency-aware readiness probes."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from w_craft_back.character_studio.models import CharacterGenerationJob
from w_craft_back.movie.poster.models import PosterGenerationJob


Component = dict[str, str | bool]


def _ok(**details: str | bool) -> Component:
    return {"status": "ok", **details}


def _failed(reason: str, **details: str | bool) -> Component:
    return {"status": "failed", "reason": reason, **details}


def _database_check() -> Component:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - probe boundary must return 503
        return _failed("unavailable")
    return _ok()


def _storage_check() -> Component:
    try:
        # Read-only sentinel lookup: no probe object or orphan is created.
        default_storage.exists("__health__/readiness")
    except Exception:  # noqa: BLE001 - backend-specific storage failures
        return _failed("unavailable")
    return _ok()


def _generation_jobs_check() -> Component:
    try:
        # Verifies that both canonical queue tables exist and are queryable.
        CharacterGenerationJob.objects.order_by().values("job_id").first()
        PosterGenerationJob.objects.order_by().values("id").first()
    except Exception:  # noqa: BLE001 - includes missing migrations
        return _failed("unavailable", worker_mode="in_process")
    return _ok(worker_mode="in_process")


def _executable_exists(value: str) -> bool:
    path = Path(value).expanduser()
    return path.is_file() or shutil.which(value) is not None


def _model3d_worker_check() -> Component:
    if not getattr(settings, "READINESS_REQUIRE_MODEL3D_WORKER", True):
        return {"status": "skipped", "required": False}

    explicit_python = str(
        getattr(settings, "MODEL3D_RECONSTRUCTION_PYTHON", "") or ""
    ).strip()
    configured_conda = str(getattr(settings, "MODEL3D_CONDA_EXE", "") or "").strip()
    runtime = explicit_python or configured_conda
    if not runtime:
        runtime = str(Path.home() / "miniconda3" / "Scripts" / "conda.exe")
    if not _executable_exists(runtime):
        return _failed("runtime_unavailable", worker_mode="detached_process")

    tools_root = Path(settings.MODEL3D_RECONSTRUCTION_TOOLS_ROOT)
    required_tools = (
        "prepare_hunyuan_views.py",
        "run_hunyuan_multiview.py",
        "postprocess_hunyuan_mesh.py",
    )
    if any(not (tools_root / name).is_file() for name in required_tools):
        return _failed("tools_unavailable", worker_mode="detached_process")
    if not Path(settings.MODEL3D_HUNYUAN_ROOT).is_dir():
        return _failed("provider_runtime_unavailable", worker_mode="detached_process")
    if not Path(settings.MODEL3D_MODEL_ROOT).is_dir():
        return _failed("models_unavailable", worker_mode="detached_process")
    return _ok(worker_mode="detached_process")


_READINESS_CHECKS: tuple[tuple[str, Callable[[], Component]], ...] = (
    ("database", _database_check),
    ("storage", _storage_check),
    ("generation_jobs", _generation_jobs_check),
    ("model3d_worker", _model3d_worker_check),
)


def _response(payload: dict, *, status: int) -> JsonResponse:
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def liveness(_request) -> JsonResponse:
    """Process-only probe; deliberately performs no dependency I/O."""

    return _response({"status": "ok"}, status=200)


@require_GET
def readiness(_request) -> JsonResponse:
    """Verify dependencies needed before this process receives traffic."""

    components = {name: check() for name, check in _READINESS_CHECKS}
    ready = all(
        component["status"] in {"ok", "skipped"}
        for component in components.values()
    )
    return _response(
        {
            "status": "ok" if ready else "unavailable",
            "components": components,
        },
        status=200 if ready else 503,
    )
