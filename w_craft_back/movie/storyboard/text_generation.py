"""User-scoped text routes for paid storyboard generation."""

from collections.abc import Mapping
from typing import Any

from w_craft_back.personal_api.access import (
    credential_for_candidate,
    generation_source,
    reject_candidate,
    resolve_generation_access,
    validate_generation_access,
)
from w_craft_back.personal_api.catalog import model_catalog
from w_craft_back.personal_api.errors import PersonalAPIError
from w_craft_back.services.text_generation.registry import FREE_TEXT_ROUTES

from .errors import StoryboardError
from .shot_list import (
    AIShotListService,
    LiteLLMShotListProvider,
    ShotListModelOption,
    _load_litellm,
    _model_option,
    _normalize_model_id,
)


def resolve_shot_list_access(
    actor: Any, *, model: str | None, source: str | None = None,
    approval_token: str | None = None,
) -> dict:
    """A selected free route is exempt; an unknown price never grants access."""
    normalized = _normalize_model_id(model) if model else None
    return resolve_generation_access(
        actor, "text", source=source, model_key=normalized,
        operation="structured", free_platform=normalized in FREE_TEXT_ROUTES,
        approval_token=approval_token,
    )


class PersonalShotListProvider:
    """Only a confirmed rejection can advance an approved personal route."""

    def __init__(self, access: Mapping) -> None:
        self.access = dict(access)
        self.model = self.access["candidates"][0]["modelKey"]

    def suggest(self, *, prompt: str, schema: Mapping) -> Mapping:
        validate_generation_access(self.access)
        last_error: Exception | None = None
        for candidate in self.access["candidates"]:
            try:
                key = credential_for_candidate(self.access, candidate)
            except PersonalAPIError as error:
                last_error = error
                continue
            self.model = candidate["modelKey"]
            provider = LiteLLMShotListProvider(self.model, api_key=key)
            try:
                return provider.suggest(prompt=prompt, schema=schema)
            except StoryboardError as error:
                # A timeout, malformed response or 5xx may already be billed.
                if error.upstream_status not in {401, 403, 404, 429}:
                    raise
                if error.upstream_status == 401:
                    reject_candidate(self.access, candidate, reason="authentication")
                last_error = error
        if last_error is not None:
            raise last_error
        raise PersonalAPIError(
            "PERSONAL_API_NOT_CONFIGURED", "No personal text route is available.", 403,
        )


def shot_list_service(
    *, access: Mapping, model: str | None = None, actor: Any = None,
) -> AIShotListService:
    """Recheck the pinned owner before workers or synchronous callers send."""
    validate_generation_access(access, user=actor)
    if access["source"] == "personal":
        return AIShotListService(provider=PersonalShotListProvider(access))
    if access.get("freePlatform") and model not in FREE_TEXT_ROUTES:
        raise PersonalAPIError(
            "PERSONAL_API_MODEL_UNAVAILABLE", "The saved free model is unavailable.",
        )
    return AIShotListService(model=model)


def shot_list_options(
    *, actor: Any, generation_source_override: str | None = None, **kwargs: Any,
) -> dict:
    """Catalog availability follows the user's source, not server API keys."""
    result = AIShotListService.options(**kwargs)
    source = generation_source_override or generation_source(actor, "text")
    litellm = _load_litellm()
    if source == "personal":
        rows = []
        prompt = AIShotListService._prompt(
            kwargs["context"], kwargs["max_shots"], kwargs["source"],
            kwargs.get("language", "ru"),
        )
        for model in model_catalog():
            if model["modality"] != "text":
                continue
            option = _model_option(model["key"], litellm)
            reason = None
            try:
                resolve_shot_list_access(actor, model=model["key"], source=source)
            except PersonalAPIError as error:
                reason = error.code
            if litellm is None:
                reason = "dependencyMissing"
            rows.append(ShotListModelOption(
                model_id=model["key"], label=model["label"],
                provider=option.provider, available=reason is None,
                unavailable_reason=reason,
            ).as_public_dict(
                litellm=litellm, prompt=prompt, max_shots=kwargs["max_shots"],
            ))
        result["models"] = rows
    else:
        for row in result["models"]:
            try:
                resolve_shot_list_access(actor, model=row["id"], source=source)
            except PersonalAPIError as error:
                row["available"] = False
                row["unavailableReason"] = error.code
    result["defaultModel"] = next(
        (row["id"] for row in result["models"] if row["available"]),
        result["defaultModel"],
    )
    return result
