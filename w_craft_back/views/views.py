"""Shared helpers for image generation responses + the public
``create_image_from_string`` entry point.

The actual provider selection lives in
:mod:`w_craft_back.services.image_generation` — this module is just the
thin bridge that the legacy poster views still call.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

from django.http import HttpResponse
from dotenv import load_dotenv
from PIL import Image

# Re-export ``ImageProviderError`` and the legacy Gemini-kind mapper so existing
# imports keep working:
#     from w_craft_back.views.views import ImageProviderError
from w_craft_back.services.image_generation import (  # noqa: F401
    ImageProviderError,
    map_to_provider_error,
    resolve_provider_for_user,
)
from w_craft_back.services.image_generation.errors import (  # noqa: F401
    _gemini_kind_to_provider_error,
)

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)


def img2response(image):
    if isinstance(image, Image.Image):
        resized_image = image
    elif isinstance(image, dict):
        logger.debug("img2response dict keys=%s", list(image.keys()))
        f = image['b64_json']
        image_data = base64.b64decode(f)

        image = Image.open(io.BytesIO(image_data))
        resized_image = image.resize((500, 500))
        logger.info(resized_image.size)

    buffered = io.BytesIO()
    resized_image.save(buffered, format="PNG")
    f = base64.b64encode(buffered.getvalue()).decode('utf-8')

    response: HttpResponse = HttpResponse(f, content_type='image/png')
    return response


# Map a poster_format key to the aspect_ratio string the resolver expects.
_ASPECT_BY_POSTER_FORMAT = {
    "vertical": "3:4",
    "square": "1:1",
    "horizontal": "16:9",
}


def _aspect_for(poster_format: str | None) -> str:
    return _ASPECT_BY_POSTER_FORMAT.get((poster_format or "").lower(), "3:4")


def create_image_from_string(
    user_string: str,
    poster_format: str | None = None,
    *,
    user: Any = None,
    model_override: str | None = None,
) -> dict:
    """Generate one poster image via the user's selected provider.

    Returns a dict with at least ``b64_json`` so :func:`img2response` works.
    Falls back to env / registry default when ``user`` has no saved preference.
    On failure raises :class:`ImageProviderError`.
    """
    logger.info(
        'Begin generating image: poster_format=%s has_user=%s override=%s',
        poster_format, bool(user), model_override,
    )
    provider = resolve_provider_for_user(user, override=model_override)
    try:
        images = provider.generate(
            user_string,
            aspect_ratio=_aspect_for(poster_format),
            variant_count=1,
        )
    except ImageProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise map_to_provider_error(exc) from exc

    if not images:
        raise ImageProviderError(
            code="IMAGE_PROVIDER_BAD_RESPONSE",
            message="Провайдер не вернул ни одного изображения.",
            http_status=502,
        )
    return {"b64_json": base64.b64encode(images[0]).decode("ascii")}
