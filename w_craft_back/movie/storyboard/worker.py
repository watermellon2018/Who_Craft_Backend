"""Durable worker execution for one Storyboard keyframe image."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from w_craft_back.credits.services import capture_provider_generation
from w_craft_back.movie.project.dashboard_models import AssetType, ProjectAsset
from w_craft_back.movie.reference_library.providers import (
    DeterministicReferenceMockProvider,
    resolve_pinned_reference_provider,
)
from w_craft_back.movie.storyboard.lifecycle import (
    StoryboardLeaseLost,
    claim_storyboard_generation,
    fail_storyboard_generation,
    heartbeat_storyboard_generation,
    mark_storyboard_provider_started,
    mark_storyboard_provider_result_received,
    storyboard_job_lease_seconds,
)
from w_craft_back.movie.storyboard.models import (
    StoryboardGenerationStatus,
    StoryboardKeyframe,
    StoryboardKeyframeGeneration,
    StoryboardShot,
)
from w_craft_back.services.image_generation.errors import (
    ImageProviderError,
    map_to_provider_error,
)
from w_craft_back.storage_gateway import (
    StorageGatewayError,
    delete_storage_key,
    normalize_image_bytes,
    store_normalized_image,
)


class StoryboardImageProviderAdapter:
    """Apply domain references within the selected provider's current limits."""

    @staticmethod
    def generate(
        provider: Any,
        snapshot: dict[str, Any],
        *,
        on_provider_start=None,
    ) -> list[bytes]:
        prompt = str(snapshot.get("compiledPrompt") or "").strip()
        primary = snapshot.get("primary_reference")
        try:
            total_timeout = max(
                1,
                int(getattr(settings, "STORYBOARD_PROVIDER_TIMEOUT_SECONDS", 120)),
            )
        except (TypeError, ValueError):
            total_timeout = 120
        total_timeout = min(
            total_timeout,
            max(1, storyboard_job_lease_seconds() - 30),
        )
        attempts = max(1, len(getattr(provider, "specs", ())))
        timeout = max(1, total_timeout // attempts)
        if isinstance(primary, dict) and primary.get("storageKey"):
            if isinstance(provider, DeterministicReferenceMockProvider):
                if on_provider_start:
                    on_provider_start()
                return provider.generate(
                    prompt,
                    aspect_ratio="16:9",
                    variant_count=1,
                    timeout=timeout,
                )
            generate_with_reference = getattr(
                provider,
                "generate_with_reference",
                None,
            )
            if generate_with_reference is None:
                raise ImageProviderError(
                    code="IMAGE_PROVIDER_CAPABILITY_MISMATCH",
                    message="Selected model does not support image references.",
                    http_status=400,
                )
            with default_storage.open(primary["storageKey"], "rb") as source:
                source_bytes = source.read()
            if on_provider_start:
                on_provider_start()
            return generate_with_reference(
                prompt,
                source_bytes,
                mime_type=primary.get("mimeType") or "image/png",
                variant_count=1,
                timeout=timeout,
                aspect_ratio="16:9",
            )
        if on_provider_start:
            on_provider_start()
        return provider.generate(
            prompt,
            aspect_ratio="16:9",
            variant_count=1,
            timeout=timeout,
        )


def _provider_for_generation(generation: StoryboardKeyframeGeneration):
    if generation.provider == "mock":
        return DeterministicReferenceMockProvider()
    if generation.provider_snapshot:
        from w_craft_back.services.image_generation.routing import (
            provider_from_route_snapshot,
        )

        provider = provider_from_route_snapshot(generation.provider_snapshot)
    else:
        provider = resolve_pinned_reference_provider(
            actor=generation.actor,
            requested_model=generation.requested_model,
        )
    if provider.name != generation.provider or provider.model_id != generation.model:
        raise ImageProviderError(
            code="IMAGE_PROVIDER_NOT_CONFIGURED",
            message="Pinned Storyboard image provider configuration changed.",
            http_status=503,
        )
    return provider


@transaction.atomic
def _finalize(
    claimed: StoryboardKeyframeGeneration,
    *,
    stored: Any,
    provider: Any,
) -> StoryboardKeyframeGeneration:
    ownership = StoryboardKeyframeGeneration.objects.filter(
        pk=claimed.pk,
    ).values("keyframe_id", "keyframe__shot_id").first()
    if ownership is None:
        raise StoryboardLeaseLost()
    shot = StoryboardShot.objects.select_for_update().filter(
        pk=ownership["keyframe__shot_id"],
    ).first()
    if shot is None:
        raise StoryboardLeaseLost()
    list(
        StoryboardKeyframe.objects.select_for_update()
        .filter(shot=shot)
        .order_by("pk")
    )
    try:
        generation = StoryboardKeyframeGeneration.objects.select_for_update().get(
            pk=claimed.pk
        )
    except StoryboardKeyframeGeneration.DoesNotExist as error:
        raise StoryboardLeaseLost() from error
    now = timezone.now()
    if (
        claimed.lease_token is None
        or generation.lease_token != claimed.lease_token
        or generation.status != StoryboardGenerationStatus.GENERATING
        or generation.lease_expires_at is None
        or generation.lease_expires_at <= now
    ):
        raise StoryboardLeaseLost()
    keyframe = generation.keyframe
    project = keyframe.shot.storyboard.scene.project
    asset = ProjectAsset.objects.create(
        project=project,
        uploaded_by=generation.actor,
        file=stored.storage_key,
        asset_type=AssetType.STORYBOARD,
        title=(
            f"{keyframe.shot.title or 'Storyboard shot'} — "
            f"{keyframe.type} r{generation.revision}"
        )[:255],
        metadata={
            "domain": "storyboard",
            "generation_id": str(generation.pk),
            "mime_type": stored.mime_type,
            "size_bytes": stored.size_bytes,
            "sha256": stored.sha256,
            "width": stored.width,
            "height": stored.height,
        },
    )
    generation.asset = asset
    generation.selected_provider = str(provider.name or "")[:64]
    generation.selected_model = str(provider.model_id or "")[:128]
    generation.status = StoryboardGenerationStatus.READY
    generation.completed_at = now
    generation.heartbeat_at = now
    generation.lease_token = None
    generation.lease_expires_at = None
    generation.save()
    from w_craft_back.movie.storyboard.generation import (
        build_generation_snapshot,
    )

    saved_options = generation.request_snapshot.get("generationOptions")
    try:
        _, current_fingerprint = build_generation_snapshot(
            keyframe,
            generation_options=(
                saved_options if isinstance(saved_options, Mapping) else None
            ),
        )
    except Exception:  # Input deletion must not discard a paid history revision.
        current_fingerprint = ""
    if current_fingerprint == generation.request_fingerprint:
        keyframe.current_generation = generation
        keyframe.save(update_fields=["current_generation", "updated_at"])
    capture_provider_generation(
        domain="storyboard",
        job_id=str(generation.pk),
        provider=provider,
    )
    return generation


def execute_storyboard_generation(job_id=None):
    claimed = claim_storyboard_generation(job_id)
    if claimed is None:
        return None
    stored = None
    try:
        provider = _provider_for_generation(claimed)
        if not heartbeat_storyboard_generation(claimed.pk, claimed.lease_token):
            raise StoryboardLeaseLost()
        payloads = StoryboardImageProviderAdapter.generate(
            provider,
            claimed.request_snapshot,
            on_provider_start=lambda: mark_storyboard_provider_started(claimed),
        )
        mark_storyboard_provider_result_received(claimed)
        if len(payloads) != 1:
            raise ImageProviderError(
                code="IMAGE_PROVIDER_BAD_RESPONSE",
                message="Image provider returned an unexpected result count.",
                http_status=502,
            )
        image = normalize_image_bytes(payloads[0])
        if not heartbeat_storyboard_generation(claimed.pk, claimed.lease_token):
            raise StoryboardLeaseLost()
        project_id = claimed.keyframe.shot.storyboard.scene.project_id
        stored = store_normalized_image(
            image,
            namespace=f"projects/{project_id}/storyboards/keyframes",
        )
        return _finalize(claimed, stored=stored, provider=provider)
    except StoryboardLeaseLost:
        if stored:
            delete_storage_key(stored.storage_key)
        return None
    except ImageProviderError as error:
        fail_storyboard_generation(
            claimed,
            code=error.code,
            detail=error.message,
            outcome_unknown=(error.http_status == 504),
        )
    except StorageGatewayError as error:
        fail_storyboard_generation(
            claimed,
            code=error.code,
            detail=error.message,
        )
    except Exception as error:
        mapped = map_to_provider_error(error)
        fail_storyboard_generation(
            claimed,
            code=mapped.code,
            detail=mapped.message,
            outcome_unknown=(claimed.provider_started_at is not None),
        )
    if stored:
        delete_storage_key(stored.storage_key)
    return StoryboardKeyframeGeneration.objects.filter(pk=claimed.pk).first()


def execute_next_storyboard_generation():
    return execute_storyboard_generation()
