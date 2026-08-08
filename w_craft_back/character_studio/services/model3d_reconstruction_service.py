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
from datetime import timedelta
import uuid
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
from w_craft_back.character_studio.services.serialization import (
    public_generation_error_message,
    public_url,
)
from w_craft_back.observability import log_context


logger = logging.getLogger(__name__)

PIPELINE_VERSION = 5
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
ReferenceView = tuple[str, str, CharacterAsset, Path]


def _identity_source_ids(references: dict[str, CharacterAsset]) -> set[str]:
    """Return explicit identity anchors recorded by generated references."""
    source_ids = set()
    for asset in references.values():
        metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
        source_id = metadata.get("source_identity_asset_id")
        if source_id:
            source_ids.add(str(source_id))
    return source_ids


def _references_share_portrait_identity(
    references: dict[str, CharacterAsset],
) -> bool:
    """Accept views anchored to the selected portrait or its identity source."""
    portrait = references.get(CharacterAssetType.PORTRAIT)
    if portrait is None:
        return False
    portrait_metadata = (
        portrait.metadata if isinstance(portrait.metadata, dict) else {}
    )
    allowed_source_ids = {str(portrait.asset_id)}
    portrait_source_id = portrait_metadata.get("source_identity_asset_id")
    if portrait_source_id:
        allowed_source_ids.add(str(portrait_source_id))
    return _identity_source_ids(references).issubset(allowed_source_ids)


def _selected_references(character: StudioCharacter) -> dict[str, CharacterAsset]:
    """Select the current stable reference set used by the head pipeline."""
    latest = CharacterAssetService().latest_ready_by_reference_type(character)
    if any(asset_type not in latest for asset_type in REQUIRED_REFERENCE_TYPES):
        return {}
    if any(asset_type not in latest for asset_type in SIDE_REFERENCE_TYPES):
        return {}
    selected = {
        asset_type: latest[asset_type]
        for asset_type in REQUIRED_REFERENCE_TYPES
    }
    for asset_type in SIDE_REFERENCE_TYPES:
        if asset_type in latest:
            selected[asset_type] = latest[asset_type]
    if not _references_share_portrait_identity(selected):
        return {}
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
        request_payload = (
            job.request_payload if isinstance(job.request_payload, dict) else {}
        )
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
    references = (
        references
        if references is not None
        else _selected_references(character)
    )
    base = {
        "status": "missing",
        "progress": 0,
        "job_id": None,
        "asset_id": None,
        "model_url": None,
        "hair_url": None,
        "assets": {"head": None, "hair": None},
        "pipeline_version": PIPELINE_VERSION,
        "error_message": "",
    }
    if not references:
        latest = CharacterAssetService().latest_ready_by_reference_type(character)
        candidate_types = (
            *REQUIRED_REFERENCE_TYPES,
            *SIDE_REFERENCE_TYPES,
        )
        candidates = {
            asset_type: latest[asset_type]
            for asset_type in candidate_types
            if asset_type in latest
        }
        base["error_message"] = (
            "Reference views use different identity sources."
            if not _references_share_portrait_identity(candidates)
            and CharacterAssetType.PORTRAIT in candidates
            else "Required references are not ready."
        )
        return base

    fingerprint = _reference_fingerprint(references)
    job = next(_jobs_for_fingerprint(character, fingerprint), None)
    asset = _asset_for_job(job)
    asset_metadata = (
        asset.metadata if asset and isinstance(asset.metadata, dict) else {}
    )
    hair_metadata = asset_metadata.get("hair_asset", {})
    hair_url = public_url(hair_metadata.get("model_url")) \
        if isinstance(hair_metadata, dict) else None
    if job is None:
        return base

    base.update(
        {
            "progress": int(job.progress or 0),
            "job_id": str(job.job_id),
            "asset_id": str(asset.asset_id) if asset else None,
            "error_message": public_generation_error_message(
                job.error_message or (asset.error_message if asset else ""),
                job.job_type,
            ),
        }
    )
    if asset and asset.status == CharacterAssetStatus.READY and asset.image_url:
        head_url = public_url(asset.image_url)
        if hair_url:
            base.update(
                {
                    "status": "ready",
                    "progress": 100,
                    "model_url": head_url,
                    "hair_url": hair_url,
                    "assets": {
                        "head": {
                            "asset_id": str(asset.asset_id),
                            "model_url": head_url,
                            "source": "generated",
                        },
                        "hair": {
                            **hair_metadata,
                            "model_url": hair_url,
                        },
                    },
                }
            )
        else:
            base["status"] = "failed"
            base["error_message"] = "Generated hair asset was not produced."
    elif job.status == GenerationJobStatus.PROCESSING:
        base["status"] = "processing"
    elif job.status == GenerationJobStatus.QUEUED:
        base["status"] = "queued"
    elif job.status == GenerationJobStatus.CANCELLATION_REQUESTED:
        base["status"] = GenerationJobStatus.CANCELLATION_REQUESTED
    elif job.status in (
        GenerationJobStatus.FAILED,
        GenerationJobStatus.CANCELLED,
    ):
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
    actor=None,
    force_retry: bool = False,
) -> dict:
    """Create at most one active job for the character's selected references."""
    locked_character = StudioCharacter.objects.select_for_update().get(pk=character.pk)
    operation_actor = actor or locked_character.user
    references = _selected_references(locked_character)
    if not references:
        return _state(locked_character, references)
    fingerprint = _reference_fingerprint(references)

    for existing in _jobs_for_fingerprint(locked_character, fingerprint):
        asset = _asset_for_job(existing)
        asset_metadata = (
            asset.metadata if asset and isinstance(asset.metadata, dict) else {}
        )
        hair_metadata = asset_metadata.get("hair_asset", {})
        has_hair_asset = bool(
            isinstance(hair_metadata, dict)
            and hair_metadata.get("model_url")
        )
        is_ready = bool(
            asset
            and asset.status == CharacterAssetStatus.READY
            and has_hair_asset
        )
        is_broken_ready = bool(
            asset
            and asset.status == CharacterAssetStatus.READY
            and not has_hair_asset
        )
        is_active = existing.status in (
            GenerationJobStatus.QUEUED,
            GenerationJobStatus.PROCESSING,
        )
        is_failed = existing.status in (
            GenerationJobStatus.FAILED,
            GenerationJobStatus.CANCELLED,
            GenerationJobStatus.CANCELLATION_REQUESTED,
        )
        if (
            is_ready
            or is_active
            or (is_broken_ready and not force_retry)
            or (is_failed and not force_retry)
        ):
            return _state(locked_character, references)

    reference_ids = {
        asset_type: str(asset.asset_id) for asset_type, asset in references.items()
    }
    job = CharacterGenerationJob.objects.create(
        character=locked_character,
        project=locked_character.project,
        user=locked_character.user,
        actor=operation_actor,
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
        timeout_seconds=7200,
    )
    relative_path = (
        f"character-studio/model3d/{locked_character.character_id}/"
        f"{fingerprint}/head.glb"
    )
    hair_relative_path = str(
        Path(relative_path).with_name("hair.glb")
    ).replace("\\", "/")
    media_url = str(getattr(settings, "MEDIA_URL", "/media/"))
    if not media_url.endswith("/"):
        media_url += "/"
    CharacterAsset.objects.create(
        character=locked_character,
        project=locked_character.project,
        user=operation_actor,
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
            "source_identity_asset_id": str(
                references[CharacterAssetType.PORTRAIT].asset_id
            ),
            "hair_asset": {
                "storage_path": hair_relative_path,
                "model_url": f"{media_url}{hair_relative_path}",
                "source": "generated",
            },
        },
    )
    return _state(locked_character, references)


def retry_reconstruction(character: StudioCharacter, *, actor=None) -> dict:
    """Retry a failed reconstruction without duplicating active/ready work."""
    return ensure_reconstruction(
        character,
        actor=actor,
        force_retry=True,
    )


def _backend_root() -> Path:
    return Path(settings.BASE_DIR).resolve()


def dispatch_reconstruction(job_id) -> None:
    """Spawn a detached backend Python process for the long-running GPU job."""
    backend_root = _backend_root()
    log_dir = (
        Path(settings.MEDIA_ROOT).resolve()
        / "character-studio"
        / "model3d"
        / "logs"
    )
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
            subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=True, **kwargs)
    except OSError:
        logger.error(
            "model3d_worker_start_failed",
            extra={
                "job_id": job_id,
                "status": "failed",
                "error_code": "WORKER_START_FAILED",
            },
        )
        _fail_job(
            job_id,
            "Could not start reconstruction worker. Try again.",
            "WORKER_START_FAILED",
        )


def _conda_python_prefix() -> list[str]:
    explicit_python = str(
        getattr(settings, "MODEL3D_RECONSTRUCTION_PYTHON", "") or ""
    ).strip()
    if explicit_python:
        return [explicit_python]

    configured = str(getattr(settings, "MODEL3D_CONDA_EXE", "") or "").strip()
    conda_exe = (
        Path(configured)
        if configured
        else Path.home() / "miniconda3" / "Scripts" / "conda.exe"
    )
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


def _file_digest(path: Path) -> str:
    """Return a streaming digest used to identify equal reference images."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pipeline_reference_views(
    references: dict[str, CharacterAsset],
) -> tuple[list[ReferenceView], list[str]]:
    """Allocate unique saved angles to Hunyuan's cardinal input slots."""
    front_asset = references[CharacterAssetType.PORTRAIT]
    front_path = _asset_path(front_asset)
    seen_digests = {_file_digest(front_path)}
    skipped_duplicates: list[str] = []
    side_views: list[ReferenceView] = []
    side_slots = iter(("left", "right"))

    for asset_type in SIDE_REFERENCE_TYPES:
        asset = references.get(asset_type)
        if asset is None:
            continue
        path = _asset_path(asset)
        digest = _file_digest(path)
        if digest in seen_digests:
            skipped_duplicates.append(asset_type)
            continue
        seen_digests.add(digest)
        side_views.append((next(side_slots), asset_type, asset, path))

    views: list[ReferenceView] = [
        ("front", CharacterAssetType.PORTRAIT, front_asset, front_path)
    ]
    if side_views:
        views.append(side_views[0])

    back_asset = references[CharacterAssetType.BACK_VIEW]
    back_path = _asset_path(back_asset)
    back_digest = _file_digest(back_path)
    if back_digest in seen_digests:
        skipped_duplicates.append(CharacterAssetType.BACK_VIEW)
    else:
        seen_digests.add(back_digest)
        views.append(
            ("back", CharacterAssetType.BACK_VIEW, back_asset, back_path)
        )

    if len(side_views) > 1:
        views.append(side_views[1])
    return views, skipped_duplicates


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
            raise FileNotFoundError(
                f"reconstruction tool does not exist: {required_path}"
            )

    views, skipped_duplicates = _pipeline_reference_views(references)
    if len(views) != 4:
        duplicate_types = ", ".join(skipped_duplicates) or "unknown"
        raise ValidationError(
            "Four unique portrait/profile/three-quarter/back views are "
            f"required; duplicate references: {duplicate_types}."
        )
    prefix = _conda_python_prefix()
    prepared_dir = work_dir / "prepared"
    hunyuan_dir = work_dir / "hunyuan"

    prepare_command = prefix + [str(tools_root / "prepare_hunyuan_views.py")]
    hunyuan_command = prefix + [str(tools_root / "run_hunyuan_multiview.py")]
    for view_name, reference_type, _, source_path in views:
        prepare_command.extend([f"--{view_name}", str(source_path)])
        if view_name in ("left", "right"):
            prepare_command.extend([f"--{view_name}-reference-type", reference_type])
        prepared_path = prepared_dir / "inputs" / f"{view_name}.png"
        hunyuan_command.extend([f"--{view_name}", str(prepared_path)])
    prepare_command.extend(["--out-dir", str(prepared_dir)])
    command_runner(prepare_command, tools_root)
    _set_progress(job.job_id, 25)
    hunyuan_command.extend(
        [
            "--model-root",
            str(model_root),
            "--hunyuan-root",
            str(hunyuan_root),
            "--out-dir",
            str(hunyuan_dir),
            "--cpu-offload",
        ]
    )
    command_runner(hunyuan_command, hunyuan_root)
    _set_progress(job.job_id, 80)
    model3d_params = (
        job.character.model3d_params
        if isinstance(job.character.model3d_params, dict)
        else {}
    )
    hair_params = model3d_params.get("hair", {})
    skin_params = model3d_params.get("skin_color", {})
    hair_color = (
        hair_params.get("hairColor", "#1e1a18")
        if isinstance(hair_params, dict)
        else "#1e1a18"
    )
    skin_color = (
        skin_params.get("skinTone", "#d8ab8a")
        if isinstance(skin_params, dict)
        else "#d8ab8a"
    )
    command_runner(
        prefix
        + [
            str(tools_root / "postprocess_hunyuan_mesh.py"),
            "--input",
            str(hunyuan_dir / "raw" / "model.glb"),
            "--output",
            str(output_path),
            "--hair-output",
            str(output_path.with_name("hair.glb")),
            "--front-reference",
            str(prepared_dir / "inputs" / "front.png"),
            "--profile-reference",
            str(prepared_dir / "inputs" / "left.png"),
            "--hair-color",
            str(hair_color),
            "--skin-color",
            str(skin_color),
            "--preview-dir",
            str(work_dir / "previews"),
            "--metadata",
            str(hunyuan_dir / "metadata.json"),
        ],
        tools_root,
    )
    return {
        "side_reference_type": next(
            (
                asset_type
                for view_name, asset_type, _, _ in views
                if view_name == "left"
            ),
            None,
        ),
        "reference_views": [
            {
                "view": view_name,
                "reference_type": asset_type,
                "asset_id": str(asset.asset_id),
                "physical_direction_known": view_name in ("front", "back"),
                "cardinal_slot_is_approximation": view_name in ("left", "right"),
            }
            for view_name, asset_type, asset, _ in views
        ],
        "skipped_duplicate_reference_types": skipped_duplicates,
        "hair_output": str(output_path.with_name("hair.glb")),
        "back_view_contributed": any(view[0] == "back" for view in views),
        "work_dir": str(work_dir),
        "hunyuan_metadata": str(hunyuan_dir / "metadata.json"),
    }


def _set_progress(job_id, progress: int) -> None:
    job = CharacterGenerationJob.objects.filter(
        job_id=job_id,
        status=GenerationJobStatus.PROCESSING,
    ).only("lease_token", "timeout_seconds").first()
    if job is None or job.lease_token is None:
        return
    now = timezone.now()
    CharacterGenerationJob.objects.filter(
        job_id=job_id,
        status=GenerationJobStatus.PROCESSING,
        lease_token=job.lease_token,
    ).update(
        progress=progress,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=job.timeout_seconds + 30),
        updated_at=now,
    )


def _fail_job(
    job_id,
    message: str,
    code: str = "RECONSTRUCTION_FAILED",
    *,
    lease_token=None,
) -> None:
    now = timezone.now()
    filters = {"job_id": job_id}
    if lease_token is not None:
        filters.update(
            status=GenerationJobStatus.PROCESSING,
            lease_token=lease_token,
        )
    else:
        filters["status__in"] = (
            GenerationJobStatus.QUEUED,
            GenerationJobStatus.PROCESSING,
        )
    updated = CharacterGenerationJob.objects.filter(**filters).update(
        status=GenerationJobStatus.FAILED,
        error_message=message[:4000],
        error_code=code,
        failed_at=now,
        lease_token=None,
        lease_expires_at=None,
        updated_at=now,
    )
    if updated:
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
    with log_context(job_id=job_id):
        return _run_reconstruction_job(
            job_id,
            command_runner=command_runner,
        )


def _run_reconstruction_job(
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
            logger.error(
                "model3d_job_not_found",
                extra={"job_id": job_id},
            )
            return None
        if job.status == GenerationJobStatus.COMPLETED:
            return _asset_for_job(job)
        if job.status != GenerationJobStatus.QUEUED:
            return None
        if job.attempts >= job.max_attempts:
            _fail_job(job.job_id, "Reconstruction retry limit reached.")
            return None
        now = timezone.now()
        job.status = GenerationJobStatus.PROCESSING
        job.progress = 5
        job.attempts += 1
        job.lease_token = uuid.uuid4()
        job.heartbeat_at = now
        job.lease_expires_at = now + timedelta(seconds=job.timeout_seconds + 30)
        job.provider_started_at = now
        job.started_at = job.started_at or now
        job.error_message = ""
        job.error_code = ""
        job.save(
            update_fields=(
                "status",
                "progress",
                "attempts",
                "lease_token",
                "heartbeat_at",
                "lease_expires_at",
                "provider_started_at",
                "started_at",
                "error_message",
                "error_code",
                "updated_at",
            )
        )

    asset = _asset_for_job(job)
    if asset is None:
        _fail_job(
            job.job_id,
            "The reconstruction output asset is missing.",
            lease_token=job.lease_token,
        )
        return None
    try:
        request_payload = (
            job.request_payload if isinstance(job.request_payload, dict) else {}
        )
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
        hair_output_path = output_path.with_name("hair.glb").resolve()
        hair_output_path.relative_to(media_root)
        if (
            not hair_output_path.is_file()
            or hair_output_path.stat().st_size == 0
        ):
            raise RuntimeError("The reconstruction pipeline produced no hair GLB file.")

        digest = hashlib.sha256()
        with output_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        previous_metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
        previous_hair = previous_metadata.get("hair_asset", {})
        hair_params = (
            job.character.model3d_params.get("hair", {})
            if isinstance(job.character.model3d_params, dict)
            else {}
        )
        hair_metadata = {
            **(previous_hair if isinstance(previous_hair, dict) else {}),
            "asset_id": f"{asset.asset_id}:hair",
            "source": "generated",
            "generation_method": (
                "multiview_hunyuan_voxel_remesh_with_inset_backing_v3"
            ),
            "style_id": hair_params.get("hairStyle", "multiview_generated"),
            "color_hex": hair_params.get("hairColor"),
            "sha256": _file_digest(hair_output_path),
            "bytes": hair_output_path.stat().st_size,
            "coordinate_space": "head_y_up_z_front_metres",
        }
        completed_metadata = {
            **previous_metadata,
            **pipeline_metadata,
            "component": "head",
            "hair_asset": hair_metadata,
            "reference_fingerprint": fingerprint,
            "sha256": digest.hexdigest(),
            "bytes": output_path.stat().st_size,
        }
        with transaction.atomic():
            locked_job = CharacterGenerationJob.objects.select_for_update().get(
                job_id=job.job_id
            )
            if (
                locked_job.status != GenerationJobStatus.PROCESSING
                or locked_job.lease_token != job.lease_token
            ):
                return None
            locked_asset = CharacterAsset.objects.select_for_update().get(pk=asset.pk)
            locked_asset.status = CharacterAssetStatus.READY
            locked_asset.error_message = ""
            locked_asset.metadata = completed_metadata
            locked_asset.save(
                update_fields=("status", "error_message", "metadata", "updated_at")
            )
            now = timezone.now()
            locked_job.status = GenerationJobStatus.COMPLETED
            locked_job.progress = 100
            locked_job.completed_at = now
            locked_job.error_message = ""
            locked_job.error_code = ""
            locked_job.lease_token = None
            locked_job.lease_expires_at = None
            locked_job.heartbeat_at = now
            locked_job.save(
                update_fields=(
                    "status",
                    "progress",
                    "completed_at",
                    "error_message",
                    "error_code",
                    "lease_token",
                    "lease_expires_at",
                    "heartbeat_at",
                    "updated_at",
                )
            )
        return locked_asset
    except Exception:  # Worker boundary: persist every pipeline failure.
        logger.error(
            "model3d_reconstruction_failed",
            extra={
                "job_id": job.job_id,
                "status": "failed",
                "error_code": "RECONSTRUCTION_FAILED",
            },
        )
        _fail_job(
            job.job_id,
            "Reconstruction failed. Try again.",
            lease_token=job.lease_token,
        )
        return None
