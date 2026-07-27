from abc import ABC, abstractmethod
import base64
import logging
import os
import time
from typing import Any, Dict, List

import requests
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


def get_image_provider(name="mock"):
    """Pick a character-studio provider by name.

    Legacy keys (``mock``/``gemini``/``google``/``imagen``) keep their existing
    behavior. Any key registered in :data:`MODEL_REGISTRY`
    (e.g. ``gemini-flash-image``, ``openrouter-flash-image``) is dispatched to
    :class:`LiteLLMCharacterProvider`, which uses the unified LiteLLM client.
    """
    from w_craft_back.services.image_generation import MODEL_REGISTRY

    raw = (name or "mock").strip()
    lower = raw.lower()
    if lower == "mock":
        return MockProvider()
    if lower in {"gemini", "google", "imagen"}:
        return GeminiProvider()
    if raw in MODEL_REGISTRY:
        return LiteLLMCharacterProvider(raw)
    raise ProviderUserFacingError(
        f"Unknown image generation provider: {raw}.",
        error_code="PROVIDER_CONFIGURATION_ERROR",
    )


class LiteLLMCharacterProvider(AIImageProvider):
    """LiteLLM-backed character-studio provider.

    Reuses the same image bytes-to-disk save pattern as :class:`GeminiProvider`
    so the rest of the pipeline (asset persistence, URL building) keeps working
    unchanged.
    """

    model_name = "litellm"

    def __init__(self, registry_key: str) -> None:
        from w_craft_back.services.image_generation import (
            LiteLLMProvider,
            resolve_model,
        )

        self.spec = resolve_model(registry_key)
        self.provider = LiteLLMProvider(self.spec)
        self.model_version = self.spec.model_id
        self.aspect_ratio = os.getenv("GEMINI_IMAGE_ASPECT_RATIO", "3:4")
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

        prompt = compiled_prompt["positive_prompt"]
        image_type = compiled_prompt.get("metadata", {}).get("image_type", "portrait")
        count = max(1, min(int(variant_count or 4), 4))
        try:
            images = self.provider.generate_with_reference(
                prompt,
                reference_image_bytes,
                mime_type=mime_type or "image/png",
                variant_count=count,
                timeout=_provider_timeout(job),
            )
            _provider_heartbeat(job)
        except ImageProviderError as exc:
            raise ProviderUserFacingError(exc.message, error_code=exc.code) from exc
        except Exception as exc:  # noqa: BLE001
            mapped = map_to_provider_error(exc)
            raise ProviderUserFacingError(mapped.message, error_code=mapped.code) from exc

        # Top-up if the model returned fewer variants than asked (same pattern as _generate).
        while len(images) < count:
            try:
                _provider_heartbeat(job)
                extra = self.provider.generate_with_reference(
                    prompt,
                    reference_image_bytes,
                    mime_type=mime_type or "image/png",
                    variant_count=1,
                    timeout=_provider_timeout(job),
                )
                _provider_heartbeat(job)
            except Exception:  # noqa: BLE001
                break
            if not extra:
                break
            images.extend(extra)
        images = images[:count]

        return self._persist_variants(
            job, images, prompt=prompt, prefix="reference", image_type=image_type
        )

    # -------- internals ---------------------------------------------------

    def _generate(self, job, *, prompt, variant_count, prefix, image_type):
        from w_craft_back.services.image_generation import (
            ImageProviderError,
            map_to_provider_error,
        )

        count = max(1, min(int(variant_count or 4), 4))
        try:
            images = self.provider.generate(
                prompt,
                aspect_ratio=self.aspect_ratio,
                variant_count=count,
                timeout=_provider_timeout(job),
            )
            _provider_heartbeat(job)
        except ImageProviderError as exc:
            raise ProviderUserFacingError(exc.message, error_code=exc.code) from exc
        except Exception as exc:  # noqa: BLE001
            mapped = map_to_provider_error(exc)
            raise ProviderUserFacingError(mapped.message, error_code=mapped.code) from exc

        # If the chat-image model returned fewer variants than requested, top up
        # by repeating extra generations (cheaper than failing).
        while len(images) < count:
            try:
                _provider_heartbeat(job)
                extra = self.provider.generate(
                    prompt,
                    aspect_ratio=self.aspect_ratio,
                    variant_count=1,
                    timeout=_provider_timeout(job),
                )
                _provider_heartbeat(job)
            except Exception:  # noqa: BLE001
                break
            if not extra:
                break
            images.extend(extra)
        images = images[:count]

        return self._persist_variants(
            job, images, prompt=prompt, prefix=prefix, image_type=image_type
        )

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
                        "provider": "litellm",
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


class GeminiProvider(AIImageProvider):
    """
    Gemini/Imagen image generation provider.

    Uses Google Generative Language API (Imagen) via REST and writes returned
    base64 images into MEDIA_ROOT.
    """

    model_name = "gemini-imagen"
    # Imagen 3 has been shut down; default to Imagen 4.
    model_version = "imagen-4.0-generate-001"

    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.model_version = os.getenv("GEMINI_IMAGE_MODEL", self.model_version)
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
            raise RuntimeError(self._format_error(resp)) from exc
        data = resp.json()

        predictions = data.get("predictions") or []
        if not predictions:
            self._raise_if_blocked(data)
            self.logger.warning(
                "_generate empty predictions: image_type=%s prompt_prefix=%r response=%s",
                image_type, prompt[:120], data,
            )
            raise RuntimeError(
                f"Gemini/Imagen returned no predictions for '{image_type}'. "
                f"This may indicate a safety filter hit or an invalid API parameter. "
                f"Response: {data}"
            )

        variants: List[Dict[str, Any]] = []
        for idx, pred in enumerate(predictions[:count]):
            encoded = (
                pred.get("bytesBase64Encoded")
                or pred.get("image", {}).get("bytesBase64Encoded")
                or pred.get("imageBytes")
            )
            if not encoded:
                raise RuntimeError(
                    f"Gemini/Imagen prediction missing base64 image bytes: {pred}"
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
            raise RuntimeError(self._format_error(response)) from exc

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
        return (
            "Gemini/Imagen request failed "
            f"({response.status_code} {response.reason}): {data}"
        )

    def _raise_if_blocked(self, data: Dict[str, Any]) -> None:
        feedback = data.get("promptFeedback") or {}
        block_reason = feedback.get("blockReason")
        if block_reason:
            raise ProviderContentBlockedError(
                CONTENT_BLOCKED_MESSAGE,
                error_code=f"GEMINI_{block_reason}",
            )
