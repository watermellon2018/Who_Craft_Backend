"""Audio route decisions with personal credentials kept out of job snapshots."""

from collections.abc import Mapping
from typing import Any

from w_craft_back.personal_api.access import (
    generation_source,
    resolve_generation_access,
    validate_generation_access,
)
from w_craft_back.personal_api.errors import PersonalAPIError

from .providers.model_registry import (
    SNAPSHOT_VERSION,
    ResolvedAudioModel,
    default_audio_model_key,
    get_audio_model_spec,
    resolve_audio_model,
)
from .providers.base import MusicProviderError


PERSONAL_AUDIO_PROVIDERS = {
    "stability": "stability",
    "google-lyria": "gemini",
    "openrouter-lyria": "openrouter",
    "elevenlabs-music-v2": "elevenlabs",
    "minimax-music-3": "minimax",
}


def resolve_personal_audio_model(candidate: Mapping) -> ResolvedAudioModel:
    """Select only a catalog adapter matching the owner's chosen service."""
    spec = get_audio_model_spec(str(candidate.get("modelKey") or ""))
    for route in spec.routes:
        if PERSONAL_AUDIO_PROVIDERS.get(route.backend_name) == candidate.get(
            "provider"
        ):
            return ResolvedAudioModel(spec, route)
    raise MusicProviderError(
        "The personal connection cannot execute this music model.",
        code="MUSIC_CAPABILITY_UNSUPPORTED", http_status=400, retryable=False,
    )


def personal_audio_snapshot(resolved: ResolvedAudioModel) -> dict[str, Any]:
    """Pin execution metadata without requiring Craft's price or server key."""
    return {
        "version": SNAPSHOT_VERSION,
        "modelKey": resolved.model.key,
        "modelDisplayName": resolved.model.display_name,
        "backendProvider": resolved.route.backend_name,
        "providerDisplayName": resolved.route.provider_display_name,
        "routeKey": resolved.route.key,
        "modelName": resolved.route.model_id,
        "capabilities": resolved.model.capabilities.as_public_dict(),
        "pricing": {"source": "personal", "unitCostUsd": "0"},
        "estimatedCostUsd": "0",
    }


def resolve_music_generation_snapshot(
    user,
    *,
    model_key: str | None = None,
    generation_source_choice: str | None = None,
    approval_token: str | None = None,
    variant_count: int = 1,
    duration_seconds: int = 30,
) -> dict[str, Any]:
    """Resolve access before credential/price checks and freeze approved routes."""
    source = generation_source_choice or (
        "personal" if approval_token else generation_source(user, "music")
    )
    selected_key = model_key or (
        default_audio_model_key() if source == "platform" else None
    )
    free = selected_key == "mock"
    access = resolve_generation_access(
        user, "music", source=source, model_key=selected_key,
        free_platform=free,
        approval_token=approval_token,
    )
    if access["source"] == "personal":
        routes = [
            {
                "candidate": candidate,
                "snapshot": personal_audio_snapshot(
                    resolve_personal_audio_model(candidate)
                ),
            }
            for candidate in access["candidates"]
        ]
        snapshot = dict(routes[0]["snapshot"])
        snapshot["generationCandidates"] = routes
        snapshot["generationCandidateIndex"] = 0
    else:
        resolved = resolve_audio_model(selected_key)
        snapshot = resolved.snapshot(variant_count, duration_seconds=duration_seconds)
    snapshot["generationAccess"] = access
    return snapshot


def validate_music_snapshot_access(snapshot: Mapping, *, user) -> Mapping:
    """Old paid jobs must also pass the current platform entitlement gate."""
    access = snapshot.get("generationAccess")
    if access and access.get("source") == "personal":
        validate_generation_access(access, user=user)
        return access
    return resolve_generation_access(
        user, "music", source="platform",
        free_platform=snapshot.get("backendProvider") == "mock",
    )


def personal_music_available(user, model_key: str, provider: str | None = None) -> bool:
    """Public catalogs expose configured personal routes, never secret values."""
    try:
        access = resolve_generation_access(
            user, "music", source="personal", model_key=model_key,
        )
    except PersonalAPIError:
        return False
    return any(
        provider is None or candidate["provider"] == provider
        for candidate in access["candidates"]
    )
