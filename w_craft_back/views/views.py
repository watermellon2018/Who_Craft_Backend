import base64
import io
import logging
import os
import requests


from django.http import HttpResponse
from dotenv import load_dotenv
from PIL import Image

# Load environment variables from .env file
load_dotenv()
NVIDIA_KEY = os.getenv('NVIDIA_KEY')
NVIDIA_BASE_URL = os.getenv('NVIDIA_BASE_URL', 'https://api.nvcf.nvidia.com')
NVIDIA_FUNCTION_ID = os.getenv('NVIDIA_FUNCTION_ID')

logger = logging.getLogger(__name__)


def img2response(image):
    if isinstance(image, Image.Image):
        resized_image = image
    elif isinstance(image, dict):
        print(image.keys())
        f = image['b64_json']  # nvidia
        image_data = base64.b64decode(f)

        image = Image.open(io.BytesIO(image_data))
        resized_image = image.resize((500, 500))
        logger.info(resized_image.size)

    buffered = io.BytesIO()
    resized_image.save(buffered, format="PNG")
    f = base64.b64encode(buffered.getvalue()).decode('utf-8')

    response: HttpResponse = HttpResponse(f, content_type='image/png')
    return response


class ImageProviderError(Exception):
    """Structured error from the image generation provider.

    Carries enough info for the view layer to render a clean JSON response
    (``code``/``message``/``http_status``) and for ops to debug the upstream
    failure (``provider_status``/``provider_body``) — without leaking the API
    key or other secrets.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 502,
        provider_status: int | None = None,
        provider_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.provider_status = provider_status
        self.provider_body = provider_body


def _mask_secret(value: str | None) -> str:
    """Return a log-safe fingerprint of an API key (first/last 4 only)."""
    if not value:
        return "<missing>"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def _gemini_kind_to_provider_error(exc) -> "ImageProviderError":
    """Translate ``GeminiImageError`` into the public ``ImageProviderError``."""
    kind = exc.kind
    provider_status = exc.provider_status
    provider_body = exc.provider_body

    if kind == "not_configured":
        return ImageProviderError(
            code="IMAGE_PROVIDER_NOT_CONFIGURED",
            message=(
                "Не настроен API ключ провайдера генерации изображений. "
                "Задайте GEMINI_API_KEY в окружении."
            ),
            http_status=503,
        )
    if kind == "forbidden":
        return ImageProviderError(
            code="IMAGE_PROVIDER_FORBIDDEN",
            message=(
                "Провайдер генерации изображений отклонил запрос. "
                "Проверьте GEMINI_API_KEY, доступ проекта к Imagen API и квоты."
            ),
            http_status=502,
            provider_status=provider_status,
            provider_body=provider_body,
        )
    if kind == "unavailable":
        return ImageProviderError(
            code="IMAGE_PROVIDER_UNAVAILABLE",
            message="Провайдер генерации изображений недоступен. Попробуйте позже.",
            http_status=503,
            provider_status=provider_status,
            provider_body=provider_body,
        )
    if kind == "blocked":
        return ImageProviderError(
            code="IMAGE_PROVIDER_BLOCKED",
            message=(
                "Промпт был заблокирован фильтрами безопасности Gemini. "
                "Переформулируйте описание."
            ),
            http_status=400,
            provider_status=provider_status,
            provider_body=provider_body,
        )
    if kind in ("empty", "bad_response"):
        return ImageProviderError(
            code="IMAGE_PROVIDER_BAD_RESPONSE",
            message="Провайдер генерации изображений вернул некорректный ответ.",
            http_status=502,
            provider_status=provider_status,
            provider_body=provider_body,
        )
    return ImageProviderError(
        code="IMAGE_PROVIDER_ERROR",
        message=f"Провайдер генерации изображений вернул ошибку (HTTP {provider_status}).",
        http_status=502,
        provider_status=provider_status,
        provider_body=provider_body,
    )


def _create_image_via_gemini(user_string: str, poster_format: str | None):
    """Generate one image via Gemini/Imagen and return the dict shape that
    ``img2response`` expects (``{"b64_json": ...}``)."""
    # Imported lazily so a test that patches ``create_image_from_string`` at
    # the call site doesn't pull network deps.
    from w_craft_back.movie.poster.gemini_image import (
        GeminiImageError,
        generate_image_via_gemini,
    )

    try:
        png_bytes = generate_image_via_gemini(
            user_string, poster_format=poster_format
        )
    except GeminiImageError as exc:
        raise _gemini_kind_to_provider_error(exc) from exc
    return {"b64_json": base64.b64encode(png_bytes).decode("ascii")}


def _create_image_via_nvidia(user_string: str):
    """Legacy NVIDIA NVCF path. Kept for back-compat — opt in by setting
    ``POSTER_IMAGE_PROVIDER=nvidia``."""
    if not NVIDIA_KEY:
        raise ImageProviderError(
            code="IMAGE_PROVIDER_NOT_CONFIGURED",
            message=(
                "Не настроен API ключ провайдера генерации изображений. "
                "Задайте NVIDIA_KEY в окружении."
            ),
            http_status=503,
        )
    if not NVIDIA_FUNCTION_ID:
        raise ImageProviderError(
            code="IMAGE_PROVIDER_NOT_CONFIGURED",
            message=(
                "Не настроен function id провайдера генерации изображений. "
                "Задайте NVIDIA_FUNCTION_ID в окружении."
            ),
            http_status=503,
        )

    invoke_url = f"{NVIDIA_BASE_URL}/v2/nvcf/pexec/functions/{NVIDIA_FUNCTION_ID}"
    fetch_url_format = f"{NVIDIA_BASE_URL}/v2/nvcf/pexec/status/"
    headers = {
        "Authorization": f"Bearer {NVIDIA_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": user_string,
        "negative_prompt": "anime",
        "sampler": "DPM",
        "seed": 0,
        "guidance_scale": 5,
        "inference_steps": 25,
    }
    session = requests.Session()
    try:
        response = session.post(invoke_url, headers=headers, json=payload, timeout=120)
        while response.status_code == 202:
            request_id = response.headers.get("NVCF-REQID")
            fetch_url = fetch_url_format + (request_id or "")
            response = session.get(fetch_url, headers=headers, timeout=120)
    except requests.RequestException as exc:
        logger.error(
            "Image provider transport error: %s url=%s key=%s",
            exc, invoke_url, _mask_secret(NVIDIA_KEY),
        )
        raise ImageProviderError(
            code="IMAGE_PROVIDER_UNAVAILABLE",
            message="Провайдер генерации изображений недоступен. Попробуйте позже.",
            http_status=503,
        ) from exc

    if response.status_code in (401, 403):
        body_preview = (response.text or "")[:1000]
        logger.error(
            "Image provider rejected request: status=%s url=%s key=%s body=%s",
            response.status_code, invoke_url, _mask_secret(NVIDIA_KEY), body_preview,
        )
        raise ImageProviderError(
            code="IMAGE_PROVIDER_FORBIDDEN",
            message=(
                "Провайдер генерации изображений отклонил запрос. "
                "Проверьте NVIDIA API key, доступ к function id и настройки провайдера."
            ),
            http_status=502,
            provider_status=response.status_code,
            provider_body=body_preview,
        )

    if response.status_code >= 400:
        body_preview = (response.text or "")[:1000]
        logger.error(
            "Image provider error: status=%s url=%s body=%s",
            response.status_code, invoke_url, body_preview,
        )
        raise ImageProviderError(
            code="IMAGE_PROVIDER_ERROR",
            message=(
                f"Провайдер генерации изображений вернул ошибку "
                f"(HTTP {response.status_code})."
            ),
            http_status=502,
            provider_status=response.status_code,
            provider_body=body_preview,
        )

    try:
        return response.json()
    except ValueError as exc:
        logger.error("Image provider returned non-JSON body: %s", response.text[:500])
        raise ImageProviderError(
            code="IMAGE_PROVIDER_BAD_RESPONSE",
            message="Провайдер генерации изображений вернул некорректный ответ.",
            http_status=502,
            provider_status=response.status_code,
        ) from exc


def create_image_from_string(user_string, poster_format: str | None = None):
    """Generate one poster image. Provider is selected by the
    ``POSTER_IMAGE_PROVIDER`` env var (default ``gemini``).

    Returns a dict with at least ``b64_json`` so ``img2response`` works for
    every provider. On failure raises :class:`ImageProviderError`.
    """
    logger.info('Begin generating...')

    provider = (os.getenv("POSTER_IMAGE_PROVIDER") or "gemini").lower()
    if provider in ("gemini", "google", "imagen"):
        return _create_image_via_gemini(user_string, poster_format)
    if provider == "nvidia":
        return _create_image_via_nvidia(user_string)
    raise ImageProviderError(
        code="IMAGE_PROVIDER_NOT_CONFIGURED",
        message=(
            f"Неизвестный провайдер генерации изображений: '{provider}'. "
            "Поддерживаются: gemini, nvidia."
        ),
        http_status=503,
    )

