"""Submit once, resume a durable operation, and publish only while fenced."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from w_craft_back.movie.project import policy
from w_craft_back.movie.project.dashboard_models import ProjectAsset
from w_craft_back.movie.project.models import Project
from w_craft_back.personal_api.access import (
    credential_for_candidate, validate_generation_access,
)
from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.services.video_generation.adapters import create_adapter
from w_craft_back.services.video_generation.contracts import (
    VideoGenerationRequest, VideoInputImage, VideoOperation, VideoProviderError,
)
from w_craft_back.services.video_generation.registry import model_from_snapshot
from w_craft_back.storage_gateway import (
    StorageGatewayError, delete_storage_key, normalize_image_bytes,
)

from .errors import VideoError, VideoLeaseLost
from .execution import execution_route
from .lifecycle import (
    checkpoint, claim_next_video_job, defer_job, finish_error, update_owned,
)
from .storage import store_generated_video

MAX_OPERATION_AGE = timedelta(hours=24)


def _adapter(job):
    actor = User.objects.filter(pk=job.actor_id, is_active=True).first()
    project = Project.objects.filter(pk=job.project_id).first()
    if not actor or not project or not policy.can_run_generation(actor, project):
        raise VideoError(
            "VIDEO_ACCESS_REVOKED", "Generation permission was revoked.", 403,
        )
    access = job.provider_snapshot["generationAccess"]
    validate_generation_access(access, user=actor)
    key = None
    if access["source"] == "personal":
        candidate = access["candidates"][job.execution_candidate_index]
        key = credential_for_candidate(access, candidate, user=actor)
    return create_adapter(execution_route(job)["provider"], api_key=key,
                          source=access["source"])


def _image(job, asset_id):
    if not asset_id:
        return None
    asset = ProjectAsset.objects.filter(pk=asset_id, project_id=job.project_id,
                                        video_generation_inputs__job=job).first()
    if not asset:
        raise VideoError("VIDEO_INPUT_UNAVAILABLE", "An input frame is unavailable.")
    with asset.file.open("rb") as handle:
        data = handle.read(10 * 1024 * 1024 + 1)
    image = normalize_image_bytes(data)
    return VideoInputImage(image.data, image.mime_type)


def _request(job):
    route = execution_route(job)
    inputs = job.input_snapshot
    model = model_from_snapshot(route["modelSpec"])
    return VideoGenerationRequest(
        model=model,
        prompt="\n\n".join(
            value for value in (inputs["description"], inputs["motionPrompt"])
            if value
        ),
        duration=route["durationSeconds"], aspect_ratio=route["aspectRatio"],
        resolution=route["resolution"], start_frame=_image(job, inputs["startAssetId"]),
        end_frame=_image(job, inputs.get("endAssetId")),
        generate_audio=model.supports_audio,
    )


def execute_next_video_job():
    job = claim_next_video_job()
    if job is None:
        return None
    try:
        return _execute_claimed_video_job(job)
    except VideoLeaseLost:
        # Error handlers also write through the fence. Losing that fence must
        # never terminate the process that services other generation queues.
        return job


def _execute_claimed_video_job(job):
    stored = None
    submitting = False
    try:
        checkpoint(job.pk, job.lease_token)
        started_at = job.provider_started_at or job.created_at
        if (job.operation_id
                and timezone.now() - started_at >= MAX_OPERATION_AGE):
            finish_error(
                job, "VIDEO_OPERATION_TIMEOUT",
                "The provider operation exceeded the 24-hour deadline. "
                "Its outcome is unknown; it will not be submitted again.",
                unknown=True,
            )
            return job
        adapter = _adapter(job)
        route = execution_route(job)
        if not job.operation_id:
            request = _request(job)
            # Persist before POST: a crashed process must not submit twice.
            update_owned(
                job.pk, job.lease_token, stage="submitting",
                provider_started_at=timezone.now(),
            )
            submitting = True
            operation = adapter.submit(request)
            if not operation.id:
                raise VideoProviderError("VIDEO_OUTCOME_UNKNOWN", outcome_unknown=True)
            update_owned(
                job.pk, job.lease_token,
                operation_id=operation.id, stage="polling",
            )
            job.operation_id = operation.id
            submitting = False
            defer_job(job)
            return job
        operation = VideoOperation(
            job.operation_id, route["provider"], route["modelKey"],
        )
        result = adapter.poll(operation)
        checkpoint(job.pk, job.lease_token)
        if result.status == "processing":
            defer_job(job)
            return job
        if result.status == "failed":
            finish_error(
                job, result.error_code or "VIDEO_PROVIDER_FAILED",
                "Video provider could not create this clip.",
            )
            return job
        if result.status != "succeeded":
            raise VideoProviderError("VIDEO_PROVIDER_RESPONSE_INVALID", retryable=True)
        update_owned(job.pk, job.lease_token, stage="downloading")
        # Re-check the pinned grant immediately before retrieving a private result.
        adapter = _adapter(job)
        download = adapter.download(result)
        checkpoint(job.pk, job.lease_token)
        stored, metadata = store_generated_video(
            download.data, download.content_type, job.project_id,
        )
        from w_craft_back.credits.video import capture_video_generation
        with transaction.atomic():
            checkpoint(job.pk, job.lease_token)
            asset = ProjectAsset.objects.create(
                project_id=job.project_id, uploaded_by=job.actor, asset_type="video",
                file=stored.storage_key, title=job.video_shot.title,
                metadata={
                    **metadata, "mimeType": stored.mime_type,
                    "sizeBytes": stored.size_bytes, "sha256": stored.sha256,
                    "videoJobId": str(job.pk),
                },
            )
            update_owned(
                job.pk, job.lease_token, result_asset_id=asset.pk,
                status="completed", stage="completed", completed_at=timezone.now(),
                lease_token=None, lease_expires_at=None, next_poll_at=None,
            )
            capture_video_generation(job)
        stored = None
    except VideoLeaseLost:
        pass
    except VideoProviderError as exc:
        if exc.outcome_unknown or (submitting and not exc.safe_to_fallback):
            finish_error(
                job, "VIDEO_OUTCOME_UNKNOWN",
                "Provider may have accepted the request. "
                "It will not be submitted again.", unknown=True,
            )
        elif exc.safe_to_fallback and _advance_candidate(job):
            pass
        elif job.operation_id and exc.retryable and job.poll_failures < 4:
            defer_job(job, failed=True)
        else:
            finish_error(job, exc.code, "Video provider request failed.")
    except PersonalAPIError as exc:
        if not job.operation_id and _advance_candidate(job):
            pass
        else:
            finish_error(job, exc.code, "Video API access is unavailable.")
    except VideoError as exc:
        finish_error(job, exc.code, "Video API or project access is unavailable.")
    except (OSError, StorageGatewayError):
        if job.operation_id and job.poll_failures < 4:
            defer_job(job, stage="downloading", failed=True)
        else:
            finish_error(
                job, "VIDEO_STORAGE_FAILED", "Could not save the generated video.",
            )
    except Exception:
        # Do not log untrusted provider bodies; the durable state is the diagnostic.
        if submitting:
            finish_error(
                job, "VIDEO_OUTCOME_UNKNOWN",
                "Provider response was lost. The request will not be repeated.",
                unknown=True,
            )
        elif job.operation_id and job.poll_failures < 4:
            defer_job(job, failed=True)
        else:
            finish_error(
                job, "VIDEO_EXECUTION_FAILED", "Video generation could not finish.",
            )
    finally:
        if stored:
            try:
                delete_storage_key(stored.storage_key)
            except OSError:
                pass
    return job


def _advance_candidate(job) -> bool:
    """Move only to an already-approved route after a definite non-acceptance."""
    snapshot = job.provider_snapshot
    access = snapshot.get("generationAccess", {})
    routes = snapshot.get("candidateRoutes", [])
    next_index = job.execution_candidate_index + 1
    if (job.operation_id or access.get("source") != "personal"
            or next_index >= len(routes)
            or next_index >= len(access.get("candidates", []))):
        return False
    update_owned(
        job.pk, job.lease_token, status="queued", stage="queued",
        execution_candidate_index=next_index, provider_started_at=None,
        lease_token=None, lease_expires_at=None, next_poll_at=None,
    )
    return True
