"""Owner-scoped image routes; job snapshots never contain API secrets."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from w_craft_back.personal_api.access import (
    credential_for_candidate,
    reject_candidate,
    resolve_generation_access,
    validate_generation_access,
)
from w_craft_back.personal_api.errors import PersonalAPIError as AccessError

from .errors import ImageProviderError
from .registry import deserialize_model_spec, resolve_model, serialize_model_spec
from .resolver import provider_from_spec, resolve_provider_for_user
from .usage import merge_usage, provider_usage_snapshot


def image_access_error(error: AccessError) -> ImageProviderError:
    return ImageProviderError(
        code=error.code,
        message=str(getattr(error, "message", error.detail)),
        http_status=error.status_code,
    )


def validate_image_access(snapshot: Mapping[str, Any], *, user=None) -> None:
    """Check the actor and revocation before any provider request or retry."""
    access = snapshot.get("generationAccess")
    if access:
        try:
            validate_generation_access(access, user=user)
        except AccessError as error:
            raise image_access_error(error) from error


def image_snapshot_for_retry(
    snapshot: Mapping[str, Any], *, user, free_platform=False,
) -> dict:
    """Personal grants stay owner-pinned; a Craft retry belongs to its new actor."""
    result = deepcopy(dict(snapshot))
    access = result.get("generationAccess", {})
    if access.get("source") == "personal":
        validate_image_access(result, user=user)
        return result
    try:
        result["generationAccess"] = resolve_generation_access(
            user, "image", source="platform",
            operation=access.get("operation", "generate"),
            free_platform=bool(access.get("freePlatform") or free_platform),
        )
    except AccessError as error:
        raise image_access_error(error) from error
    return result


def resolve_image_generation(
    user, *, source: str | None = None, model_key: str | None = None,
    operation: str = "generate",
    approval_token: str | None = None,
):
    """Gate a paid route before resolving any server-side credentials."""
    try:
        access = resolve_generation_access(
            user, "image", source=source, model_key=model_key, operation=operation,
            approval_token=approval_token,
        )
    except AccessError as error:
        raise image_access_error(error) from error
    if access["source"] == "personal":
        return PersonalImageProvider({"generationAccess": access})
    provider = resolve_provider_for_user(
        user, override=model_key, require_edit=operation == "edit",
    )
    provider.generation_access = access
    provider.provider_snapshot = {
        "generationAccess": access,
        "spec": serialize_model_spec(provider.spec),
    }
    return provider


def annotate_image_catalog(rows: list[dict], user, *, source=None) -> list[dict]:
    """Expose access for the selected payer, independent of server API keys."""
    from w_craft_back.personal_api.access import (
        generation_source, has_paid_subscription,
    )

    effective_source = source or generation_source(user, "image")
    personal_models: set[str] = set()
    if effective_source == "personal":
        try:
            access = resolve_generation_access(user, "image", source="personal")
        except AccessError:
            pass
        else:
            personal_models = {item["modelKey"] for item in access["candidates"]}
    subscribed = (
        has_paid_subscription(user) if effective_source == "platform" else False
    )
    return [
        {
            **row,
            "configured": (
                row["key"] in personal_models if effective_source == "personal"
                else bool(row.get("configured") and (
                    row.get("backend") == "mock" or subscribed
                ))
            ),
        }
        for row in rows
    ]


class PersonalImageProvider:
    """Use only the user's pinned connection/model pairs in their chosen order."""

    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self.generation_access = deepcopy(snapshot["generationAccess"])
        if self.generation_access.get("source") != "personal":
            raise ValueError("Personal image route requires personal access")
        raw_specs = snapshot.get("personalModels")
        candidates = self.generation_access["candidates"]
        self.specs = tuple(
            deserialize_model_spec(raw) for raw in raw_specs
        ) if raw_specs else tuple(
            resolve_model(candidate["modelKey"]) for candidate in candidates
        )
        if not self.specs or len(self.specs) != len(candidates) or any(
            spec.key != candidate["modelKey"]
            for spec, candidate in zip(self.specs, candidates)
        ):
            raise ValueError("Personal image route contains mismatched models")
        self.spec = self.specs[0]
        self.name = self.spec.key
        self.model_id = self.spec.model_id
        self.provider_snapshot = {
            "generationAccess": self.generation_access,
            "spec": serialize_model_spec(self.spec),
            "personalModels": [serialize_model_spec(spec) for spec in self.specs],
        }
        self._usage_events: list[dict[str, Any]] = []
        self._attempts: list[dict[str, Any]] = []
        self._active_index = 0

    def supports_edit(self) -> bool:
        return any(
            spec.supports_edit and spec.backend != "gemini-native"
            for spec in self.specs
        )

    def supports_reference(self) -> bool:
        return any(spec.supports_reference for spec in self.specs)

    @staticmethod
    def _supports_call(spec, method: str, args: tuple, kwargs: dict) -> bool:
        if method == "edit" and (
            not spec.supports_edit or spec.backend == "gemini-native"
        ):
            return False
        if method.startswith("generate_with_reference"):
            if not spec.supports_reference:
                return False
            count = len(args[1]) if method == "generate_with_references" else 1
            maximum = spec.supported_parameters.get("input_references", {}).get(
                "max", 1,
            )
            if count > maximum:
                return False
        maximum = spec.supported_parameters.get("n", {}).get("max", 4)
        return int(kwargs.get("variant_count", 1)) <= maximum

    def _invoke(self, method: str, *args: Any, **kwargs: Any):
        last_error: ImageProviderError | None = None
        for index in range(self._active_index, len(self.specs)):
            if not self._supports_call(self.specs[index], method, args, kwargs):
                last_error = ImageProviderError(
                    code="IMAGE_PROVIDER_CAPABILITY_MISMATCH",
                    message="Личная модель не поддерживает параметры этой операции.",
                    http_status=400,
                )
                continue
            candidate = self.generation_access["candidates"][index]
            try:
                secret = credential_for_candidate(self.generation_access, candidate)
            except AccessError as error:
                # Revoked or rotated credentials are skipped before an HTTP call.
                last_error = image_access_error(error)
                continue
            provider = provider_from_spec(self.specs[index], api_key=secret)
            self.spec = provider.spec
            self.name = provider.name
            self.model_id = provider.model_id
            try:
                if (
                    method == "generate_with_references"
                    and not hasattr(provider, method)
                ):
                    result = provider.generate_with_reference(
                        args[0], args[1][0], **kwargs,
                    )
                else:
                    result = getattr(provider, method)(*args, **kwargs)
            except ImageProviderError as error:
                usage = provider_usage_snapshot(provider)
                if usage:
                    self._usage_events.append(usage)
                self._attempts.append({
                    "connectionId": candidate["connectionId"],
                    "modelKey": self.spec.key, "result": "failed",
                    "errorCode": error.code,
                })
                if not error.rejected_before_acceptance:
                    raise
                reason = "authentication" if error.provider_status == 401 else (
                    "quota" if error.provider_status in {402, 429}
                    else "model_unavailable"
                )
                reject_candidate(self.generation_access, candidate, reason=reason)
                last_error = error
                continue
            usage = provider_usage_snapshot(provider)
            if usage:
                self._usage_events.append(usage)
            self._attempts.append({
                "connectionId": candidate["connectionId"],
                "modelKey": self.spec.key, "result": "succeeded", "errorCode": "",
            })
            self._active_index = index
            return result
        if last_error:
            raise last_error
        raise ImageProviderError(
            code="PERSONAL_API_UNAVAILABLE",
            message="Нет доступного личного подключения для генерации изображений.",
            http_status=403,
        )

    def generate(self, *args: Any, **kwargs: Any):
        return self._invoke("generate", *args, **kwargs)

    def edit(self, *args: Any, **kwargs: Any):
        return self._invoke("edit", *args, **kwargs)

    def generate_with_reference(self, *args: Any, **kwargs: Any):
        return self._invoke("generate_with_reference", *args, **kwargs)

    def generate_with_references(self, *args: Any, **kwargs: Any):
        return self._invoke("generate_with_references", *args, **kwargs)

    def usage_snapshot(self) -> dict[str, Any]:
        return {
            **merge_usage(self._usage_events),
            "billingSource": "personal",
            "selectedProvider": self.name, "selectedModel": self.model_id,
            "attempts": list(self._attempts),
        }
