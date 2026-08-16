from abc import ABC, abstractmethod
import base64
import logging
import os
import time
from typing import Any, Dict, List

import requests
from django.conf import settings
from requests import HTTPError

from w_craft_back.storage_gateway import (
    StorageGatewayError,
    store_image_bytes,
)


class ProviderUserFacingError(RuntimeError):
    error_code = "PROVIDER_ERROR"

    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.user_message = message
        if error_code:
            self.error_code = error_code


class ProviderContentBlockedError(ProviderUserFacingError):
    error_code = "PROVIDER_CONTENT_BLOCKED"


CONTENT_BLOCKED_MESSAGE = (
    "Gemini заблокировал промпт по правилам безопасности. "
    "Измените описание персонажа: уберите двусмысленные, сексуализированные "
    "или жестокие детали, особенно если персонаж несовершеннолетний."
)


def _provider_timeout(job) -> float:
    timeout = float(getattr(job, "timeout_seconds", 120))
    deadline = getattr(job, "provider_deadline", None)
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Provider operation exceeded its end-to-end timeout.")
    return max(0.1, min(timeout, remaining))


def _provider_heartbeat(job) -> None:
    callback = getattr(job, "provider_heartbeat", None)
    if callable(callback) and callback() is False:
        raise RuntimeError("Generation lease was lost during provider execution.")


def _store_provider_image(payload: bytes, *, namespace: str):
    try:
        return store_image_bytes(payload, namespace=namespace)
    except StorageGatewayError as exc:
        raise ProviderUserFacingError(
            "Провайдер вернул недопустимое изображение.",
            error_code="PROVIDER_BAD_IMAGE",
        ) from exc


class AIImageProvider(ABC):
    @abstractmethod
    def generate_character_variants(self, job, compiled_prompt, variant_count):
        raise NotImplementedError

    @abstractmethod
    def edit_character_region(self, job, compiled_prompt, variant_count):
        raise NotImplementedError

    @abstractmethod
    def generate_character_sheet(self, job, compiled_prompt):
        raise NotImplementedError

    def generate_from_reference(
        self,
        job,
        compiled_prompt,
        reference_image_bytes: bytes,
        mime_type: str,
        variant_count: int,
    ):
        # Default: not supported. Concrete providers override to enable image-input.
        from w_craft_back.services.image_generation.errors import (
            CODE_IMAGE_INPUT_NOT_SUPPORTED,
        )

        raise ProviderUserFacingError(
            "Этот провайдер не поддерживает генерацию по референсному изображению.",
            error_code=CODE_IMAGE_INPUT_NOT_SUPPORTED,
        )


class MockProvider(AIImageProvider):
    model_name = "mock-character-provider"
    model_version = "mvp-1"

    def generate_character_variants(self, job, compiled_prompt, variant_count):
        return self._variants(job, compiled_prompt, variant_count, "initial")

    def edit_character_region(self, job, compiled_prompt, variant_count):
        return self._variants(job, compiled_prompt, variant_count, "edit")

    def generate_character_sheet(self, job, compiled_prompt):
        return self._variants(job, compiled_prompt, 4, "sheet")

    def generate_from_reference(
        self, job, compiled_prompt, reference_image_bytes, mime_type, variant_count
    ):
        # MockProvider ignores the reference bytes; just emits placeholder variants.
        return self._variants(job, compiled_prompt, variant_count, "reference")

    # Minimal valid PNG (1x1 transparent pixel) so identity-anchored downstream
    # generation can read the mock asset's bytes off disk.
    _PLACEHOLDER_PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0l"
        "EQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def _variants(self, job, compiled_prompt, variant_count, prefix):
        safe_count = max(1, min(int(variant_count or 4), 4))
        variants = []
        image_type = compiled_prompt.get("metadata", {}).get(
            "image_type",
            "portrait",
        )
        for index in range(safe_count):
            seed = abs(
                hash(f"{job.job_id}:{index}:{prefix}:{image_type}")
            ) % 100000000
            stored = _store_provider_image(
                self._PLACEHOLDER_PNG,
                namespace=(
                    f"mock/characters/{job.character_id}/{job.job_id}/"
                    f"{image_type}"
                ),
            )
            variants.append(
                {
                    "variant_index": index,
                    "image_url": "",
                    "storage_path": stored.storage_key,
                    "width": stored.width,
                    "height": stored.height,
                    "mime_type": stored.mime_type,
                    "seed": seed,
                    "model_name": self.model_name,
                    "model_version": self.model_version,
                    "prompt": compiled_prompt["positive_prompt"],
                    "negative_prompt": compiled_prompt["negative_prompt"],
                    "metadata": {
                        "provider": "mock",
                        "prefix": prefix,
                        "image_type": image_type,
                        "sha256": stored.sha256,
                        "size_bytes": stored.size_bytes,
                    },
                }
            )
        return variants


def get_image_provider(name="mock", provider_snapshot=None):
    """Pick a character-studio provider by name.

    Legacy keys keep their existing behavior. Registry and dynamic catalog
    models are reconstructed from the persisted snapshot when available.
    """
    from w_craft_back.services.image_generation import (
        ImageProviderError,
        deserialize_model_spec,
        resolve_model,
    )

    raw = (name or "mock").strip()
    lower = raw.lower()
    if lower == "mock":
        return MockProvider()
    if lower in {"gemini", "google", "imagen"}:
        snapshot_spec = (
            provider_snapshot.get("spec")
            if isinstance(provider_snapshot, dict)
            else None
        )
        if snapshot_spec:
            if (
                not isinstance(snapshot_spec, dict)
                or snapshot_spec.get("key") != "gemini"
                or snapshot_spec.get("backend") != "gemini-legacy"
                or not isinstance(snapshot_spec.get("model_id"), str)
            ):
                raise ProviderUserFacingError(
                    "The saved image model snapshot does not match the "
                    "job provider.",
                    error_code="PROVIDER_CONFIGURATION_ERROR",
                )
            return GeminiProvider(model_version=snapshot_spec["model_id"])
        return GeminiProvider()
    if isinstance(provider_snapshot, dict) and provider_snapshot.get("candidates"):
        from w_craft_back.services.image_generation.routing import (
            provider_from_route_snapshot,
        )

        try:
            routed = provider_from_route_snapshot(provider_snapshot)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderUserFacingError(
                "The saved provider route is invalid.",
                error_code="PROVIDER_CONFIGURATION_ERROR",
            ) from exc
        if routed.spec.key != raw:
            raise ProviderUserFacingError(
                "The saved provider route does not match the job provider.",
                error_code="PROVIDER_CONFIGURATION_ERROR",
            )
        adapter = RegistryCharacterProvider(spec=routed.spec)
        adapter.provider = routed
        return adapter
    try:
        if provider_snapshot and provider_snapshot.get("spec"):
            spec = deserialize_model_spec(provider_snapshot["spec"])
        else:
            spec = resolve_model(raw)
    except ImageProviderError as exc:
        raise ProviderUserFacingError(exc.message, error_code=exc.code) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderUserFacingError(
            "The saved image model snapshot is invalid.",
            error_code="PROVIDER_CONFIGURATION_ERROR",
        ) from exc
    if spec.key != raw:
        raise ProviderUserFacingError(
            "The saved image model snapshot does not match the job provider.",
            error_code="PROVIDER_CONFIGURATION_ERROR",
        )
    if spec.backend == "mock":
        return MockProvider()
    if spec.backend == "gemini-legacy":
        return GeminiProvider()
    return RegistryCharacterProvider(spec=spec)


class RegistryCharacterProvider(AIImageProvider):
    """Character Studio adapter over the unified model registry."""

    _STRING_PARAMETERS = (
        "aspect_ratio",
        "resolution",
        "size",
        "quality",
        "output_format",
        "background",
    )

    def __init__(self, registry_key: str | None = None, *, spec=None) -> None:
        from w_craft_back.services.image_generation import (
            provider_from_spec,
            resolve_model,
        )

        self.spec = spec or resolve_model(registry_key)
        self.provider = provider_from_spec(self.spec)
        self.model_name = self.spec.backend
        self.model_version = self.spec.model_id
        self.logger = logging.getLogger(__name__)

    # -------- AIImageProvider interface -----------------------------------

    def generate_character_variants(self, job, compiled_prompt, variant_count):
        prompt = compiled_prompt["positive_prompt"]
        image_type = compiled_prompt.get("metadata", {}).get("image_type", "portrait")
        return self._generate(
            job,
            prompt=prompt,
            variant_count=variant_count,
            prefix="initial",
            image_type=image_type,
        )

    def edit_character_region(self, job, compiled_prompt, variant_count):
        # Character Studio's current "edit" job carries no source pixels. It is
        # intentionally text-to-image, so supports_generate (not supports_edit)
        # is the relevant registry capability until a real source image exists.
        prompt = compiled_prompt["positive_prompt"]
        image_type = compiled_prompt.get("metadata", {}).get("image_type", "portrait")
        return self._generate(
            job,
            prompt=prompt,
            variant_count=variant_count,
            prefix="edit",
            image_type=image_type,
        )

    def generate_character_sheet(self, job, compiled_prompt):
        prompt = compiled_prompt["positive_prompt"]
        prompt = f"{prompt}. Create a character sheet with 4 variations on pose/angle."
        return self._generate(
            job,
            prompt=prompt,
            variant_count=4,
            prefix="sheet",
            image_type="reference_sheet",
        )

    def generate_from_reference(
        self, job, compiled_prompt, reference_image_bytes, mime_type, variant_count
    ):
        from w_craft_back.services.image_generation import (
            ImageProviderError,
            map_to_provider_error,
        )
        from w_craft_back.services.image_generation.errors import (
            CODE_BAD_RESPONSE,
            CODE_IMAGE_INPUT_NOT_SUPPORTED,
        )

        if not self.spec.supports_reference:
            raise ProviderUserFacingError(
                f"Image model '{self.spec.label}' does not support reference images.",
                error_code=CODE_IMAGE_INPUT_NOT_SUPPORTED,
            )

        prompt = compiled_prompt["positive_prompt"]
        image_type = compiled_prompt.get("metadata", {}).get("image_type", "portrait")
        count = max(1, min(int(variant_count or 4), 4))
        images: list[bytes] = []
        parameters = self._provider_parameters(job)
        per_call_max = self._per_call_max(count)
        while len(images) < count:
            try:
                _provider_heartbeat(job)
                extra = self.provider.generate_with_reference(
                    prompt,
                    reference_image_bytes,
                    mime_type=mime_type or "image/png",
                    variant_count=min(count - len(images), per_call_max),
                    timeout=_provider_timeout(job),
                    **parameters,
                )
                self._sync_active_provider()
                _provider_heartbeat(job)
            except ImageProviderError as exc:
                raise ProviderUserFacingError(
                    exc.message,
                    error_code=exc.code,
                ) from exc
            except Exception as exc:  # noqa: BLE001
                mapped = map_to_provider_error(exc)
                raise ProviderUserFacingError(
                    mapped.message,
                    error_code=mapped.code,
                ) from exc
            if not extra:
                break
            images.extend(extra)
        images = images[:count]
        if len(images) != count:
            raise ProviderUserFacingError(
                "Image provider returned fewer variants than requested.",
                error_code=CODE_BAD_RESPONSE,
            )

        return self._persist_variants(
            job, images, prompt=prompt, prefix="reference", image_type=image_type
        )

    # -------- internals ---------------------------------------------------

    def _generate(self, job, *, prompt, variant_count, prefix, image_type):
        from w_craft_back.services.image_generation import (
            ImageProviderError,
            map_to_provider_error,
        )
        from w_craft_back.services.image_generation.errors import CODE_BAD_RESPONSE

        count = max(1, min(int(variant_count or 4), 4))
        images: list[bytes] = []
        parameters = self._provider_parameters(job)
        per_call_max = self._per_call_max(count)
        while len(images) < count:
            try:
                _provider_heartbeat(job)
                extra = self.provider.generate(
                    prompt,
                    variant_count=min(count - len(images), per_call_max),
                    timeout=_provider_timeout(job),
                    **parameters,
                )
                self._sync_active_provider()
                _provider_heartbeat(job)
            except ImageProviderError as exc:
                raise ProviderUserFacingError(
                    exc.message,
                    error_code=exc.code,
                ) from exc
            except Exception as exc:  # noqa: BLE001
                mapped = map_to_provider_error(exc)
                raise ProviderUserFacingError(
                    mapped.message,
                    error_code=mapped.code,
                ) from exc
            if not extra:
                break
            images.extend(extra)
        images = images[:count]
        if len(images) != count:
            raise ProviderUserFacingError(
                "Image provider returned fewer variants than requested.",
                error_code=CODE_BAD_RESPONSE,
            )

        return self._persist_variants(
            job, images, prompt=prompt, prefix=prefix, image_type=image_type
        )

    def _per_call_max(self, requested_count: int) -> int:
        descriptor = (self.spec.supported_parameters or {}).get("n") or {}
        raw_maximum = descriptor.get("max")
        if not isinstance(raw_maximum, (int, float)) or isinstance(
            raw_maximum,
            bool,
        ):
            return 1
        maximum = int(raw_maximum)
        try:
            configured_maximum = int(
                getattr(settings, "IMAGE_PROVIDER_MAX_OUTPUT_IMAGES", 4)
            )
        except (TypeError, ValueError):
            configured_maximum = 4
        if configured_maximum <= 0:
            configured_maximum = 4
        return max(
            1,
            min(requested_count, maximum, configured_maximum, 10),
        )

    def _sync_active_provider(self) -> None:
        active_spec = getattr(self.provider, "spec", None)
        if active_spec is None:
            return
        self.spec = active_spec
        self.model_name = active_spec.backend
        self.model_version = active_spec.model_id

    def _provider_parameters(self, job) -> dict[str, object]:
        payload = job.request_payload if isinstance(job.request_payload, dict) else {}
        supported = self.spec.supported_parameters or {}
        parameters: dict[str, object] = {}
        for key in self._STRING_PARAMETERS:
            value = payload.get(key)
            if key in supported and isinstance(value, str) and value.strip():
                parameters[key] = value.strip()

        compression = payload.get("output_compression")
        if (
            "output_compression" in supported
            and isinstance(compression, int)
            and not isinstance(compression, bool)
            and 0 <= compression <= 100
        ):
            parameters["output_compression"] = compression

        seed = payload.get("seed")
        if (
            "seed" in supported
            and isinstance(seed, int)
            and not isinstance(seed, bool)
        ):
            parameters["seed"] = seed

        if "aspect_ratio" in supported and "aspect_ratio" not in parameters:
            default_ratio = os.getenv("GEMINI_IMAGE_ASPECT_RATIO", "").strip()
            if default_ratio:
                parameters["aspect_ratio"] = default_ratio
        return parameters

    def _persist_variants(self, job, images, *, prompt, prefix, image_type):
        variants: List[Dict[str, Any]] = []
        for idx, image_bytes in enumerate(images):
            stored = _store_provider_image(
                image_bytes,
                namespace=(
                    f"character-studio/jobs/{job.job_id}/"
                    f"{prefix}-{image_type}"
                ),
            )
            variants.append(
                {
                    "variant_index": idx,
                    "image_url": "",
                    "storage_path": stored.storage_key,
                    "width": stored.width,
                    "height": stored.height,
                    "mime_type": stored.mime_type,
                    "seed": None,
                    "model_name": self.model_name,
                    "model_version": self.model_version,
                    "prompt": prompt,
                    "negative_prompt": "",
                    "metadata": {
                        "provider": self.spec.backend,
                        "registry_key": self.spec.key,
                        "model_id": self.spec.model_id,
                        "mode": self.spec.mode,
                        "prefix": prefix,
                        "image_type": image_type,
                        "sha256": stored.sha256,
                        "size_bytes": stored.size_bytes,
                    },
                }
            )
        return variants


# Import compatibility for callers/tests that still use the previous name.
LiteLLMCharacterProvider = RegistryCharacterProvider


class GeminiProvider(AIImageProvider):
    """
    Gemini/Imagen image generation provider.

    Uses Google Generative Language API (Imagen) via REST and writes returned
    base64 images into MEDIA_ROOT.
    """

    model_name = "gemini-imagen"
    # Imagen 3 has been shut down; default to Imagen 4.
    model_version = "imagen-4.0-generate-001"

    def __init__(self, *, model_version: str | None = None):
        self.api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.model_version = model_version or os.getenv(
            "GEMINI_IMAGE_MODEL",
            self.model_version,
        )
        self.endpoint_base = os.getenv(
            "GEMINI_API_BASE_URL",
            "https://generativelanguage.googleapis.com",
        )
        self.aspect_ratio = os.getenv("GEMINI_IMAGE_ASPECT_RATIO", "3:4")
        self.image_size = os.getenv("GEMINI_IMAGE_SIZE", "")
        self.person_generation = os.getenv("GEMINI_PERSON_GENERATION", "allow_adult")
        self.send_negative_prompt = os.getenv(
            "GEMINI_SEND_NEGATIVE_PROMPT", ""
        ).lower() in {"1", "true", "yes", "on"}
        self.translate_prompt = os.getenv(
            "GEMINI_TRANSLATE_PROMPT", "true"
        ).lower() not in {"0", "false", "no", "off"}
        self.text_model = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
        self.safety_filter_level = os.getenv("GEMINI_SAFETY_FILTER_LEVEL", "block_few")
        self.logger = logging.getLogger(__name__)

    def generate_character_variants(self, job, compiled_prompt, variant_count):
        prompt = compiled_prompt["positive_prompt"]
        image_type = compiled_prompt.get("metadata", {}).get("image_type", "portrait")
        negative = compiled_prompt.get("negative_prompt") or ""
        return self._generate(
            job,
            prompt=prompt,
            negative_prompt=negative,
            variant_count=variant_count,
            prefix="initial",
            image_type=image_type,
        )

    def edit_character_region(self, job, compiled_prompt, variant_count):
        # MVP: synthesize a new image if true image-edit is unavailable.
        # NOTE: edit_instruction is kept in DB metadata for revision history,
        # but is NOT concatenated into the image prompt — it contains "key: value"
        # text that the model would render as labels on the picture.
        prompt = compiled_prompt["positive_prompt"]
        image_type = compiled_prompt.get("metadata", {}).get("image_type", "portrait")
        negative = compiled_prompt.get("negative_prompt") or ""
        return self._generate(
            job,
            prompt=prompt,
            negative_prompt=negative,
            variant_count=variant_count,
            prefix="edit",
            image_type=image_type,
        )

    def generate_character_sheet(self, job, compiled_prompt):
        prompt = compiled_prompt["positive_prompt"]
        negative = compiled_prompt.get("negative_prompt") or ""
        prompt = f"{prompt}. Create a character sheet with 4 variations on pose/angle."
        return self._generate(
            job,
            prompt=prompt,
            negative_prompt=negative,
            variant_count=4,
            prefix="sheet",
            image_type="reference_sheet",
        )

    def _generate(
        self,
        job,
        prompt: str,
        negative_prompt: str,
        variant_count: int,
        prefix: str,
        image_type: str = "portrait",
    ):
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set.")

        count = max(1, min(int(variant_count or 4), 4))
        url = f"{self.endpoint_base}/v1beta/models/{self.model_version}:predict"
        # Ignore broken local proxy env vars in Windows/dev environments.
        session = requests.Session()
        session.trust_env = False
        prompt = self._prepare_prompt(
            session,
            prompt,
            timeout_seconds=_provider_timeout(job),
        )
        _provider_heartbeat(job)
        payload: Dict[str, Any] = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "sampleCount": count,
                "aspectRatio": self.aspect_ratio,
                "personGeneration": self.person_generation,
            },
        }
        if self.image_size:
            payload["parameters"]["imageSize"] = self.image_size
        if self.safety_filter_level:
            payload["parameters"]["safetyFilterLevel"] = self.safety_filter_level
        if negative_prompt and self.send_negative_prompt:
            payload["parameters"]["negativePrompt"] = negative_prompt

        resp = session.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=_provider_timeout(job),
        )
        _provider_heartbeat(job)
        try:
            resp.raise_for_status()
        except HTTPError as exc:
            raise ProviderUserFacingError(
                self._format_error(resp),
                error_code="PROVIDER_HTTP_ERROR",
            ) from exc
        data = resp.json()

        predictions = data.get("predictions") or []
        if not predictions:
            self._raise_if_blocked(data)
            self.logger.warning(
                "image_provider_empty_predictions",
                extra={
                    "image_type": image_type,
                    "provider": "gemini",
                },
            )
            raise ProviderUserFacingError(
                "Провайдер не вернул изображение.",
                error_code="PROVIDER_EMPTY_RESPONSE",
            )

        variants: List[Dict[str, Any]] = []
        for idx, pred in enumerate(predictions[:count]):
            encoded = (
                pred.get("bytesBase64Encoded")
                or pred.get("image", {}).get("bytesBase64Encoded")
                or pred.get("imageBytes")
            )
            if not encoded:
                raise ProviderUserFacingError(
                    "Провайдер вернул некорректный ответ.",
                    error_code="PROVIDER_BAD_RESPONSE",
                )
            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise ProviderUserFacingError(
                    "Провайдер вернул некорректное изображение.",
                    error_code="PROVIDER_BAD_IMAGE",
                ) from exc
            stored = _store_provider_image(
                image_bytes,
                namespace=(
                    f"character-studio/jobs/{job.job_id}/"
                    f"{prefix}-{image_type}"
                ),
            )
            variants.append(
                {
                    "variant_index": idx,
                    "image_url": "",
                    "storage_path": stored.storage_key,
                    "width": stored.width,
                    "height": stored.height,
                    "mime_type": stored.mime_type,
                    "seed": pred.get("seed"),
                    "model_name": self.model_name,
                    "model_version": self.model_version,
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "metadata": {
                        "provider": "gemini",
                        "prefix": prefix,
                        "image_type": image_type,
                        "sha256": stored.sha256,
                        "size_bytes": stored.size_bytes,
                    },
                }
            )
        return variants

    def _prepare_prompt(
        self,
        session,
        prompt: str,
        *,
        timeout_seconds: int,
    ) -> str:
        if not self.translate_prompt or not self._contains_non_ascii(prompt):
            return prompt

        url = f"{self.endpoint_base}/v1beta/models/{self.text_model}:generateContent"
        translation_request = (
            "Translate this image-generation prompt to concise English. "
            "Preserve character details, style words, and camera terms. "
            "Return only the translated prompt:\n\n"
            f"{prompt}"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": translation_request}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
            },
        }
        response = session.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=min(timeout_seconds, 60),
        )
        try:
            response.raise_for_status()
        except HTTPError as exc:
            raise ProviderUserFacingError(
                self._format_error(response),
                error_code="PROVIDER_HTTP_ERROR",
            ) from exc

        data = response.json()
        self._raise_if_blocked(data)
        parts = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        translated = " ".join(part.get("text", "") for part in parts).strip()
        if not translated:
            raise ProviderUserFacingError(
                "Gemini не вернул перевод промпта. Попробуйте упростить описание персонажа или написать промпт на английском.",
                error_code="PROVIDER_TRANSLATION_EMPTY",
            )
        return translated

    def _contains_non_ascii(self, value: str) -> bool:
        return any(ord(char) > 127 for char in value)

    def _headers(self) -> Dict[str, str]:
        return {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def _format_error(self, response) -> str:
        try:
            data = response.json()
        except ValueError:
            data = response.text
        if isinstance(data, dict):
            self._raise_if_blocked(data)
        return f"Провайдер отклонил запрос (HTTP {response.status_code})."

    def _raise_if_blocked(self, data: Dict[str, Any]) -> None:
        feedback = data.get("promptFeedback") or {}
        block_reason = feedback.get("blockReason")
        if block_reason:
            raise ProviderContentBlockedError(
                CONTENT_BLOCKED_MESSAGE,
                error_code="PROVIDER_CONTENT_BLOCKED",
            )
