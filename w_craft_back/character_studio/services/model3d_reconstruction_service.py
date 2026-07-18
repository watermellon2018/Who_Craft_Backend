"""Per-character Hunyuan3D head reconstruction orchestration.

The HTTP request only creates an idempotent database job.  A detached Django
management command performs the GPU-heavy pipeline in the ``basic`` conda
environment and updates the same job/asset rows as it progresses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
    CharacterGenerationJob,
    GenerationJobStatus,
    GenerationJobType,
    StudioCharacter,
)
from w_craft_back.character_studio.services.asset_service import (
    CharacterAssetService,
)
from w_craft_back.character_studio.services.errors import ValidationError
from w_craft_back.character_studio.services.serialization import public_url


logger = logging.getLogger(__name__)

PIPELINE_VERSION = 3
RECONSTRUCTION_PROVIDER = "local_hunyuan3d"
REQUIRED_REFERENCE_TYPES = (
    CharacterAssetType.PORTRAIT,
    CharacterAssetType.FULL_BODY,
    CharacterAssetType.BACK_VIEW,
)
SIDE_REFERENCE_TYPES = (
    CharacterAssetType.PROFILE,
    CharacterAssetType.THREE_QUARTER,
)

CommandRunner = Callable[[list[str], Path], None]


def _selected_references(character: StudioCharacter) -> dict[str, CharacterAsset]:
    """Select the current stable reference set used by the head pipeline."""
    latest = CharacterAssetService().latest_ready_by_reference_type(character)
    if any(asset_type not in latest for asset_type in REQUIRED_REFERENCE_TYPES):
        return {}
    side = next(
        (latest[asset_type] for asset_type in SIDE_REFERENCE_TYPES if asset_type in latest),
        None,
    )
    if side is None:
        return {}
    selected = {asset_type: latest[asset_type] for asset_type in REQUIRED_REFERENCE_TYPES}
    selected[side.asset_type] = side
    return selected


def _reference_fingerprint(references: dict[str, CharacterAsset]) -> str:
    """Hash immutable asset identities and checksums for idempotent jobs."""
    rows = []
    for asset_type, asset in sorted(references.items()):
        metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
        rows.append(
            {
                "asset_id": str(asset.asset_id),
                "asset_type": asset_type,
                "sha256": metadata.get("sha256", ""),
                "version": int(asset.version or 1),
            }
        )
    encoded = json.dumps(
        {"pipeline_version": PIPELINE_VERSION, "references": rows},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jobs_for_fingerprint(character: StudioCharacter, fingerprint: str):
    for job in character.generation_jobs.filter(
        job_type=GenerationJobType.MODEL3D_RECONSTRUCTION,
    ).order_by("-created_at"):
        request_payload = job.request_payload if isinstance(job.request_payload, dict) else {}
        if request_payload.get("reference_fingerprint") == fingerprint:
            yield job


def _asset_for_job(job: CharacterGenerationJob | None) -> CharacterAsset | None:
    if job is None:
        return None
    return CharacterAsset.objects.filter(
        character=job.character,
        asset_type=CharacterAssetType.MODEL_3D,
        source_job_id=job.job_id,
    ).order_by("-created_at").first()


def _state(
    character: StudioCharacter,
    references: dict[str, CharacterAsset] | None = None,
) -> dict:
    """Serialize the latest reconstruction state for the editor."""
    references = references if references is not None else _selected_references(character)
    base = {
        "status": "missing",
        "progress": 0,
        "job_id": None,
        "asset_id": None,
        "model_url": None,
        "error_message": "",
    }
    if not references:
        base["error_message"] = "Required references are not ready."
        return base

    fingerprint = _reference_fingerprint(references)
    job = next(_jobs_for_fingerprint(character, fingerprint), None)
    asset = _asset_for_job(job)
    if job is None:
        return base

    base.update(
        {
            "progress": int(job.progress or 0),
            "job_id": str(job.job_id),
            "asset_id": str(asset.asset_id) if asset else None,
            "error_message": job.error_message or (asset.error_message if asset else ""),
        }
    )
    if asset and asset.status == CharacterAssetStatus.READY and asset.image_url:
        base.update(
            {
                "status": "ready",
                "progress": 100,
                "model_url": public_url(asset.image_url),
            }
        )
    elif job.status == GenerationJobStatus.PROCESSING:
        base["status"] = "processing"
    elif job.status == GenerationJobStatus.QUEUED:
        base["status"] = "queued"
    elif job.status in (GenerationJobStatus.FAILED, GenerationJobStatus.CANCELLED):
        base["status"] = "failed"
    elif job.status == GenerationJobStatus.COMPLETED:
        base.update(
            {
                "status": "failed",
                "error_message": base["error_message"] or "3D asset was not produced.",
            }
        )
    return base


def reconstruction_state(character: StudioCharacter, *, ensure: bool = False) -> dict:
    """Return state, optionally creating the first job for a locked character."""
    if ensure and character.status == "references_locked":
        return ensure_reconstruction(character)
    return _state(character)


@transaction.atomic
def ensure_reconstruction(
    character: StudioCharacter,
    *,
    force_retry: bool = False,
) -> dict:
    """Create at most one active job for the character's selected references."""
    locked_character = StudioCharacter.objects.select_for_update().get(pk=character.pk)
    references = _selected_references(locked_character)
    if not references:
        return _state(locked_character, references)
    fingerprint = _reference_fingerprint(references)

    for existing in _jobs_for_fingerprint(locked_character, fingerprint):
        asset = _asset_for_job(existing)
        is_ready = bool(asset and asset.status == CharacterAssetStatus.READY)
        is_active = existing.status in (
            GenerationJobStatus.QUEUED,
            GenerationJobStatus.PROCESSING,
        )
        is_failed = existing.status in (
            GenerationJobStatus.FAILED,
            GenerationJobStatus.CANCELLED,
        )
        if is_ready or is_active or (is_failed and not force_retry):
            return _state(locked_character, references)

    reference_ids = {
        asset_type: str(asset.asset_id) for asset_type, asset in references.items()
    }
    job = CharacterGenerationJob.objects.create(
        character=locked_character,
        project=locked_character.project,
        user=locked_character.user,
        job_type=GenerationJobType.MODEL3D_RECONSTRUCTION,
        status=GenerationJobStatus.QUEUED,
        variant_count=1,
        request_payload={
            "pipeline_version": PIPELINE_VERSION,
            "reference_fingerprint": fingerprint,
            "reference_asset_ids": reference_ids,
        },
        provider=RECONSTRUCTION_PROVIDER,
        model_name="tencent/Hunyuan3D-2mv",
        model_version="3a761b539b29fe4ff64714813aa9560fd66f5de0",
        progress=0,
    )
    relative_path = (
        f"character-studio/model3d/{locked_character.character_id}/"
        f"{fingerprint}/head.glb"
    )
    media_url = str(getattr(settings, "MEDIA_URL", "/media/"))
    if not media_url.endswith("/"):
        media_url += "/"
    CharacterAsset.objects.create(
        character=locked_character,
        project=locked_character.project,
        user=locked_character.user,
        asset_type=CharacterAssetType.MODEL_3D,
        image_url=f"{media_url}{relative_path}",
        storage_path=relative_path,
        mime_type="model/gltf-binary",
        source="reconstructed",
        source_job_id=job.job_id,
        model_name=job.model_name,
        model_version=job.model_version,
        status=CharacterAssetStatus.GENERATING,
        metadata={
            "pipeline_version": PIPELINE_VERSION,
            "reference_fingerprint": fingerprint,
            "reference_asset_ids": reference_ids,
        },
    )
    transaction.on_commit(lambda: dispatch_reconstruction(job.job_id))
    return _state(locked_character, references)


def retry_reconstruction(character: StudioCharacter) -> dict:
    """Retry a failed reconstruction without duplicating active/ready work."""
    return ensure_reconstruction(character, force_retry=True)


def _backend_root() -> Path:
    return Path(settings.BASE_DIR).resolve()


def dispatch_reconstruction(job_id) -> None:
    """Spawn a detached backend Python process for the long-running GPU job."""
    backend_root = _backend_root()
    log_dir = Path(settings.MEDIA_ROOT).resolve() / "character-studio" / "model3d" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(backend_root / "manage.py"),
        "run_model3d_reconstruction",
        "--job-id",
        str(job_id),
    ]
    kwargs = {
        "cwd": str(backend_root),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    try:
        with (log_dir / f"{job_id}.log").open("ab") as output:
            subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, **kwargs)
    except OSError as error:
        logger.exception("Could not start model3d reconstruction job %s", job_id)
        _fail_job(job_id, f"Could not start reconstruction worker: {error}", "WORKER_START_FAILED")


def _conda_python_prefix() -> list[str]:
    explicit_python = str(
        getattr(settings, "MODEL3D_RECONSTRUCTION_PYTHON", "") or ""
    ).strip()
    if explicit_python:
        return [explicit_python]

    configured = str(getattr(settings, "MODEL3D_CONDA_EXE", "") or "").strip()
    conda_exe = Path(configured) if configured else Path.home() / "miniconda3" / "Scripts" / "conda.exe"
    if not conda_exe.is_file():
        raise FileNotFoundError(
            "conda executable was not found; set MODEL3D_CONDA_EXE or "
            "MODEL3D_RECONSTRUCTION_PYTHON"
        )
    environment = str(getattr(settings, "MODEL3D_CONDA_ENV", "basic") or "basic")
    return [
        str(conda_exe),
        "run",
        "--no-capture-output",
        "-n",
        environment,
        "python",
    ]


def _setting_path(name: str, default: Path) -> Path:
    return Path(getattr(settings, name, default)).expanduser().resolve()


def _asset_path(asset: CharacterAsset) -> Path:
    """Resolve a reference only inside MEDIA_ROOT (never trust DB paths)."""
    media_root = Path(settings.MEDIA_ROOT).resolve()
    raw = Path(asset.storage_path)
    candidate = raw if raw.is_absolute() else media_root / raw
    resolved = candidate.resolve()
    try:
        resolved.relative_to(media_root)
    except ValueError as error:
        raise ValidationError("Reference asset path escapes MEDIA_ROOT.") from error
    if not resolved.is_file():
        raise FileNotFoundError(f"Reference asset file does not exist: {resolved}")
    return resolved


def _run_command(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=str(cwd), check=True)


def _execute_pipeline(
    job: CharacterGenerationJob,
    references: dict[str, CharacterAsset],
    output_path: Path,
    work_dir: Path,
    command_runner: CommandRunner,
) -> dict:
    """Execute prepare -> Hunyuan -> normalize using the configured runtime."""
    tools_root = _setting_path(
        "MODEL3D_RECONSTRUCTION_TOOLS_ROOT",
        _backend_root().parent / "who_craft" / "tools" / "reconstruction",
    )
    hunyuan_root = _setting_path(
        "MODEL3D_HUNYUAN_ROOT",
        _backend_root().parent / "external" / "Hunyuan3D-2",
    )
    model_root = _setting_path("MODEL3D_MODEL_ROOT", hunyuan_root / "models")
    for required_path in (
        tools_root / "prepare_hunyuan_views.py",
        tools_root / "run_hunyuan_multiview.py",
        tools_root / "postprocess_hunyuan_mesh.py",
    ):
        if not required_path.is_file():
            raise FileNotFoundError(f"reconstruction tool does not exist: {required_path}")

    portrait = _asset_path(references[CharacterAssetType.PORTRAIT])
    side = next(references[asset_type] for asset_type in SIDE_REFERENCE_TYPES if asset_type in references)
    side_path = _asset_path(side)
    prefix = _conda_python_prefix()
    prepared_dir = work_dir / "prepared"
    hunyuan_dir = work_dir / "hunyuan"

    command_runner(
        prefix
        + [
            str(tools_root / "prepare_hunyuan_views.py"),
            "--front",
            str(portrait),
            "--left",
            str(side_path),
            "--out-dir",
            str(prepared_dir),
        ],
        tools_root,
    )
    _set_progress(job.job_id, 25)
    command_runner(
        prefix
        + [
            str(tools_root / "run_hunyuan_multiview.py"),
            "--front",
            str(prepared_dir / "inputs" / "front.png"),
            "--left",
            str(prepared_dir / "inputs" / "left.png"),
            "--model-root",
            str(model_root),
            "--hunyuan-root",
            str(hunyuan_root),
            "--out-dir",
            str(hunyuan_dir),
            "--cpu-offload",
        ],
        hunyuan_root,
    )
    _set_progress(job.job_id, 80)
    command_runner(
        prefix
        + [
            str(tools_root / "postprocess_hunyuan_mesh.py"),
            "--input",
            str(hunyuan_dir / "raw" / "model.glb"),
            "--output",
            str(output_path),
            "--preview-dir",
            str(work_dir / "previews"),
            "--metadata",
            str(hunyuan_dir / "metadata.json"),
        ],
        tools_root,
    )
    return {
        "side_reference_type": side.asset_type,
        "work_dir": str(work_dir),
        "hunyuan_metadata": str(hunyuan_dir / "metadata.json"),
    }


def _set_progress(job_id, progress: int) -> None:
    CharacterGenerationJob.objects.filter(job_id=job_id).update(progress=progress)


def _fail_job(job_id, message: str, code: str = "RECONSTRUCTION_FAILED") -> None:
    now = timezone.now()
    CharacterGenerationJob.objects.filter(job_id=job_id).update(
        status=GenerationJobStatus.FAILED,
        error_message=message[:4000],
        error_code=code,
        failed_at=now,
    )
    CharacterAsset.objects.filter(
        source_job_id=job_id,
        asset_type=CharacterAssetType.MODEL_3D,
    ).update(
        status=CharacterAssetStatus.FAILED,
        error_message=message[:4000],
        updated_at=now,
    )


def run_reconstruction_job(
    job_id,
    *,
    command_runner: CommandRunner = _run_command,
) -> CharacterAsset | None:
    """Claim and execute one queued reconstruction job."""
    with transaction.atomic():
        try:
            job = CharacterGenerationJob.objects.select_for_update().select_related(
                "character",
            ).get(job_id=job_id, job_type=GenerationJobType.MODEL3D_RECONSTRUCTION)
        except CharacterGenerationJob.DoesNotExist:
            logger.error("Unknown model3d reconstruction job %s", job_id)
            return None
        if job.status == GenerationJobStatus.COMPLETED:
            return _asset_for_job(job)
        if job.status != GenerationJobStatus.QUEUED:
            return None
        job.status = GenerationJobStatus.PROCESSING
        job.progress = 5
        job.started_at = timezone.now()
        job.error_message = ""
        job.error_code = ""
        job.save(
            update_fields=(
                "status",
                "progress",
                "started_at",
                "error_message",
                "error_code",
            )
        )

    asset = _asset_for_job(job)
    if asset is None:
        _fail_job(job.job_id, "The reconstruction output asset is missing.")
        return None
    try:
        request_payload = job.request_payload if isinstance(job.request_payload, dict) else {}
        reference_ids = request_payload.get("reference_asset_ids", {})
        if not isinstance(reference_ids, dict):
            raise ValidationError("Reconstruction reference list is invalid.")
        assets = CharacterAsset.objects.filter(
            character=job.character,
            asset_id__in=list(reference_ids.values()),
            status=CharacterAssetStatus.READY,
        )
        references = {item.asset_type: item for item in assets}
        if len(references) != len(reference_ids):
            raise ValidationError("One or more locked references are unavailable.")

        output_path = Path(settings.MEDIA_ROOT).resolve() / asset.storage_path
        media_root = Path(settings.MEDIA_ROOT).resolve()
        output_path = output_path.resolve()
        output_path.relative_to(media_root)
        fingerprint = str(request_payload.get("reference_fingerprint", ""))
        work_dir = output_path.parent / "work"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pipeline_metadata = _execute_pipeline(
            job,
            references,
            output_path,
            work_dir,
            command_runner,
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("The reconstruction pipeline produced no GLB file.")

        digest = hashlib.sha256()
        with output_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        asset.status = CharacterAssetStatus.READY
        asset.error_message = ""
        asset.metadata = {
            **(asset.metadata if isinstance(asset.metadata, dict) else {}),
            **pipeline_metadata,
            "reference_fingerprint": fingerprint,
            "sha256": digest.hexdigest(),
            "bytes": output_path.stat().st_size,
        }
        asset.save(update_fields=("status", "error_message", "metadata", "updated_at"))
        now = timezone.now()
        CharacterGenerationJob.objects.filter(job_id=job.job_id).update(
            status=GenerationJobStatus.COMPLETED,
            progress=100,
            completed_at=now,
            error_message="",
            error_code="",
        )
        return asset
    except Exception as error:  # Worker boundary: persist every pipeline failure.
        logger.exception("Model3d reconstruction failed for job %s", job.job_id)
        _fail_job(job.job_id, str(error) or error.__class__.__name__)
        return None
