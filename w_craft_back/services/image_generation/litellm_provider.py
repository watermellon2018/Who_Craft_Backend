"""LiteLLM-backed :class:`ImageProvider`.

Two code paths:
    * ``mode="image"`` — uses ``litellm.image_generation`` (Imagen 4, DALL-E, …).
    * ``mode="chat"``  — uses ``litellm.completion`` with image-out modality
      (Gemini 2.5 Flash Image / OpenRouter chat-image variants).

Edit always goes through chat-completions: LiteLLM's ``image_edit`` only
covers OpenAI's edit API today, while Gemini's "Nano Banana" — the model
we actually rely on for editing — accepts image-in/image-out via chat.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any

from django.conf import settings

from .errors import (
    CODE_BAD_RESPONSE,
    CODE_EDIT_NOT_SUPPORTED,
    CODE_IMAGE_INPUT_NOT_SUPPORTED,
    ImageProviderError,
    map_to_provider_error,
)
from .registry import ModelSpec

logger = logging.getLogger(__name__)


# Map aspect-ratio hints to a ``size`` string LiteLLM/OpenAI understand.
_SIZE_BY_ASPECT: dict[str, str] = {
    "1:1": "1024x1024",
    "square": "1024x1024",
    "3:4": "1024x1365",
    "vertical": "1024x1365",
    "4:3": "1365x1024",
    "16:9": "1792x1024",
    "horizontal": "1792x1024",
    "9:16": "1024x1792",
}


def _size_for(aspect_ratio: str | None) -> str:
    return _SIZE_BY_ASPECT.get((aspect_ratio or "").strip().lower(), "1024x1024")


_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.DOTALL)


def _provider_output_limit() -> int:
    try:
        configured = getattr(
            settings, "IMAGE_PROVIDER_MAX_OUTPUT_BYTES", 20 * 1024 * 1024
        )
        value = int(configured)
    except (TypeError, ValueError):
        return 20 * 1024 * 1024
    return value if value > 0 else 20 * 1024 * 1024


def _decode_b64_or_data_url(value: str) -> bytes:
    if not isinstance(value, str):
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Провайдер вернул изображение в неподдерживаемом формате.",
            http_status=502,
        )
    if value.startswith(("http://", "https://")):
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message=(
                "Провайдер вернул небезопасную удалённую ссылку "
                "вместо изображения."
            ),
            http_status=502,
        )
    match = _DATA_URL_RE.match(value)
    if match and not match.group("mime").lower().startswith("image/"):
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Провайдер вернул data URL не с изображением.",
            http_status=502,
        )
    payload = match.group("data") if match else value
    limit = _provider_output_limit()
    max_encoded_length = ((limit + 2) // 3) * 4 + 16
    if len(payload) > max_encoded_length:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Изображение провайдера превышает допустимый размер.",
            http_status=502,
        )
    try:
        decoded = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:  # pragma: no cover
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Провайдер вернул некорректные base64-данные.",
            http_status=502,
        ) from exc
    if len(decoded) > limit:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Изображение провайдера превышает допустимый размер.",
            http_status=502,
        )
    return decoded


def _provider_output_count_limit() -> int:
    try:
        value = int(getattr(settings, "IMAGE_PROVIDER_MAX_OUTPUT_IMAGES", 4))
    except (TypeError, ValueError):
        return 4
    return value if value > 0 else 4


def _provider_output_total_limit() -> int:
    try:
        value = int(
            getattr(
                settings,
                "IMAGE_PROVIDER_MAX_OUTPUT_TOTAL_BYTES",
                _provider_output_limit(),
            )
        )
    except (TypeError, ValueError):
        return _provider_output_limit()
    return value if value > 0 else _provider_output_limit()


def _append_decoded_image(images: list[bytes], value: str) -> None:
    if len(images) >= _provider_output_count_limit():
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Провайдер вернул слишком много изображений.",
            http_status=502,
        )
    decoded = _decode_b64_or_data_url(value)
    aggregate_size = sum(len(image) for image in images) + len(decoded)
    if aggregate_size > _provider_output_total_limit():
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Общий размер изображений провайдера превышает лимит.",
            http_status=502,
        )
    images.append(decoded)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Tolerant getattr/getitem — LiteLLM returns Pydantic models in some
    versions and plain dicts in others.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_chat_images(response: Any) -> list[bytes]:
    """Walk a chat-completion response and pull every image part out as bytes.

    Handles three shapes seen in the wild:
        * ``message.images = [{image_url: {url: "data:image/png;base64,..."}}]``
        * ``message.content = [{type: "image_url", image_url: {url: ...}}]``
        * raw base64 string in ``content`` (rare, but observed for Gemini).
    """
    images: list[bytes] = []
    choices = _get(response, "choices") or []
    for choice in choices:
        message = _get(choice, "message")
        if message is None:
            continue
        # LiteLLM-normalized: message.images
        for item in (_get(message, "images") or []):
            url = _get(_get(item, "image_url") or {}, "url") or _get(item, "url")
            if url:
                _append_decoded_image(images, url)
                continue
            b64 = _get(item, "b64_json") or _get(item, "data")
            if b64:
                _append_decoded_image(images, b64)
        # Multimodal content parts
        content = _get(message, "content")
        if isinstance(content, list):
            for part in content:
                part_type = _get(part, "type")
                if part_type in ("image_url", "image"):
                    url = (
                        _get(_get(part, "image_url") or {}, "url")
                        or _get(part, "url")
                    )
                    if url:
                        _append_decoded_image(images, url)
                        continue
                    inline = _get(part, "inline_data") or _get(part, "inlineData")
                    data = _get(inline or {}, "data")
                    if data:
                        _append_decoded_image(images, data)
                elif part_type == "input_image":
                    data = _get(part, "image_data") or _get(part, "data")
                    if data:
                        _append_decoded_image(images, data)
    if not images:
        logger.warning(
            "Chat-mode image extraction found no images: response_type=%s",
            type(response).__name__,
        )
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Модель не вернула изображение (вероятно, фильтр безопасности).",
            http_status=502,
        )
    return images


def _extract_image_api(response: Any) -> list[bytes]:
    """Parse inline image bytes and reject provider-controlled remote URLs."""
    data = _get(response, "data") or []
    images: list[bytes] = []
    for item in data:
        b64 = _get(item, "b64_json")
        if b64:
            _append_decoded_image(images, b64)
            continue
        url = _get(item, "url")
        if url and url.startswith("data:"):
            _append_decoded_image(images, url)
            continue
        if url:
            raise ImageProviderError(
                code=CODE_BAD_RESPONSE,
                message="Провайдер вернул удалённую ссылку вместо inline-изображения.",
                http_status=502,
            )
    if not images:
        raise ImageProviderError(
            code=CODE_BAD_RESPONSE,
            message="Провайдер вернул пустой ответ.",
            http_status=502,
        )
    return images


class LiteLLMProvider:
    """Adapter from the :class:`ImageProvider` protocol to ``litellm``."""

    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec
        self.name = spec.key
        self.model_id = spec.model_id

    def supports_edit(self) -> bool:
        return self.spec.supports_edit

    # ------------------------------------------------------------------ #
    # Generate
    # ------------------------------------------------------------------ #
    def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str | None = None,
        variant_count: int = 1,
        **kwargs: Any,
    ) -> list[bytes]:
        import litellm  # local import: optional dependency at runtime

        n = max(1, int(variant_count or 1))
        if self.spec.mode == "image":
            params: dict[str, Any] = {
                "model": self.model_id,
                "prompt": prompt,
                "n": n,
                "size": _size_for(aspect_ratio),
                "response_format": "b64_json",
            }
            extra = kwargs.get("extra_body") or {}
            if extra:
                params["extra_body"] = extra
            timeout = kwargs.get("timeout")
            if timeout is not None:
                params["timeout"] = timeout
            try:
                response = litellm.image_generation(**params)
            except Exception as exc:  # noqa: BLE001
                raise map_to_provider_error(exc) from exc
            return _extract_image_api(response)

        # mode == "chat"
        try:
            response = litellm.completion(
                model=self.model_id,
                messages=[{"role": "user", "content": prompt}],
                modalities=["image", "text"],
                n=n,
                timeout=kwargs.get("timeout"),
            )
        except Exception as exc:  # noqa: BLE001
            raise map_to_provider_error(exc) from exc
        return _extract_chat_images(response)

    # ------------------------------------------------------------------ #
    # Edit
    # ------------------------------------------------------------------ #
    def edit(
        self,
        image_bytes: bytes,
        instruction: str,
        *,
        mime_type: str = "image/png",
        **kwargs: Any,
    ) -> bytes:
        if not self.spec.supports_edit:
            raise ImageProviderError(
                code=CODE_EDIT_NOT_SUPPORTED,
                message=(
                    f"Модель '{self.spec.label}' не поддерживает редактирование. "
                    "Выберите другую модель в настройках профиля."
                ),
                http_status=400,
            )

        import litellm

        data_url = (
            f"data:{mime_type or 'image/png'};base64,"
            f"{base64.b64encode(image_bytes).decode('ascii')}"
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": instruction},
                ],
            }
        ]
        try:
            response = litellm.completion(
                model=self.model_id,
                messages=messages,
                modalities=["image", "text"],
                timeout=kwargs.get("timeout"),
            )
        except Exception as exc:  # noqa: BLE001
            raise map_to_provider_error(exc) from exc

        images = _extract_chat_images(response)
        return images[0]

    # ------------------------------------------------------------------ #
    # Generate with reference image (image-to-image)
    # ------------------------------------------------------------------ #
    def generate_with_reference(
        self,
        prompt: str,
        image_bytes: bytes,
        *,
        mime_type: str = "image/png",
        variant_count: int = 1,
        timeout: float | None = None,
    ) -> list[bytes]:
        # Image input is currently available only on chat-mode multimodal models
        # (Gemini 2.5 Flash Image, OpenRouter chat-image variants). Image-API
        # models like Imagen 4 (mode="image") accept no input image.
        if self.spec.mode != "chat" or not self.spec.supports_edit:
            raise ImageProviderError(
                code=CODE_IMAGE_INPUT_NOT_SUPPORTED,
                message=(
                    f"Модель '{self.spec.label}' не поддерживает генерацию "
                    "по референсному "
                    "изображению. Выберите модель с поддержкой image-input."
                ),
                http_status=400,
            )

        import litellm

        data_url = (
            f"data:{mime_type or 'image/png'};base64,"
            f"{base64.b64encode(image_bytes).decode('ascii')}"
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        n = max(1, int(variant_count or 1))
        try:
            response = litellm.completion(
                model=self.model_id,
                messages=messages,
                modalities=["image", "text"],
                n=n,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise map_to_provider_error(exc) from exc
        return _extract_chat_images(response)
