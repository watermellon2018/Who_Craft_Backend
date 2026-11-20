"""Select Sound Effects access without consulting another account's API key."""

from collections.abc import Mapping

from w_craft_back.personal_api.access import (
    resolve_generation_access,
    validate_generation_access,
)

from .providers.elevenlabs import MODEL_KEY, MODEL_NAME
from .providers.registry import get_sound_effect_provider


def resolve_sound_effect_generation_snapshot(
    user, *, generation_source=None, duration_seconds=None, approval_token=None,
) -> dict:
    access = resolve_generation_access(
        user, "sound_effect", source=generation_source, model_key=MODEL_KEY,
        approval_token=approval_token,
    )
    if access["source"] == "personal":
        snapshot = {
            "version": "sound-effect-provider-v1",
            "modelKey": MODEL_KEY,
            "backendProvider": "elevenlabs-sfx",
            "providerDisplayName": "ElevenLabs Sound Effects",
            "modelName": MODEL_NAME,
            "pricing": {"source": "personal"},
            "estimatedCostUsd": "0",
            "generationCandidateIndex": 0,
        }
    else:
        snapshot = get_sound_effect_provider().provider_snapshot(duration_seconds)
    snapshot["generationAccess"] = access
    return snapshot


def validate_sound_effect_snapshot_access(snapshot: Mapping, *, user) -> Mapping:
    access = snapshot.get("generationAccess")
    if access and access.get("source") == "personal":
        validate_generation_access(access, user=user)
        return access
    return resolve_generation_access(user, "sound_effect", source="platform")
