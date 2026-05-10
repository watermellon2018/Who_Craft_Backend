"""Legacy poster generation endpoints (``/api/generate/poster/`` and
``/api/generate/edit/``).

The dashboard-style API for the new poster page lives in
``movie/poster/dashboard_views.py``. This module continues to serve the
single-shot text-to-image flow used by older FE call sites — but now with:

  * structured error handling for provider failures (no more 500s),
  * a real prompt builder that respects style + format,
  * multipart support so the FE can post a reference image,
  * accept rules that include JPG.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

from django.http import HttpResponse, JsonResponse
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from w_craft_back.movie.poster.file_validation import (
    ReferenceImageValidationError,
    validate_reference_image,
)
from w_craft_back.movie.poster.prompts import (
    ALLOWED_FORMATS,
    ALLOWED_STYLES,
    DEFAULT_FORMAT,
    DEFAULT_STYLE,
    PROMPT_MAX_DESCRIPTION_LENGTH,
    build_poster_prompt,
)
from w_craft_back.views import img2response
from w_craft_back.views.views import (
    ImageProviderError,
    _gemini_kind_to_provider_error,
    create_image_from_string,
)
from w_craft_back.movie.poster.gemini_image import (
    GeminiImageError,
    edit_image_via_gemini,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _error_json(
    code: str,
    message: str,
    http_status: int = status.HTTP_400_BAD_REQUEST,
) -> Response:
    return Response(
        {"success": False, "error": {"code": code, "message": message}},
        status=http_status,
    )


def _read_inputs(request) -> tuple[str, str, str, Optional[object]]:
    """Pull (description, style, format, reference_file) from a request that
    might be a GET (query string), JSON POST, or multipart POST."""
    if request.method == "GET":
        params = request.GET
        description = (
            params.get("prompt") or params.get("description") or ""
        )
        style = params.get("style") or DEFAULT_STYLE
        poster_format = params.get("format") or DEFAULT_FORMAT
        reference = None
    else:
        data = request.data if hasattr(request, "data") else {}
        description = (
            data.get("prompt")
            or data.get("description")
            or ""
        )
        style = data.get("style") or DEFAULT_STYLE
        poster_format = data.get("format") or DEFAULT_FORMAT
        reference = (
            request.FILES.get("referenceImage")
            or request.FILES.get("reference_image")
            or request.FILES.get("file")
            if hasattr(request, "FILES") else None
        )

    return description, style, poster_format, reference


def _validate(description: str, style: str, poster_format: str) -> Optional[Response]:
    desc = (description or "").strip()
    if not desc:
        return _error_json(
            "POSTER_PROMPT_REQUIRED",
            "Опишите идею постера перед генерацией.",
        )
    if len(desc) > PROMPT_MAX_DESCRIPTION_LENGTH:
        return _error_json(
            "POSTER_PROMPT_TOO_LONG",
            f"Описание должно быть не длиннее {PROMPT_MAX_DESCRIPTION_LENGTH} символов.",
        )
    if style not in ALLOWED_STYLES:
        return _error_json(
            "INVALID_POSTER_STYLE",
            "Недопустимый стиль постера.",
        )
    if poster_format not in ALLOWED_FORMATS:
        return _error_json(
            "INVALID_POSTER_FORMAT",
            "Недопустимый формат постера.",
        )
    return None


def _provider_error_response(exc: ImageProviderError) -> Response:
    return Response(
        {
            "success": False,
            "error": {"code": exc.code, "message": exc.message},
        },
        status=exc.http_status,
    )


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #

@api_view(['GET', 'POST'])
@parser_classes([JSONParser, FormParser, MultiPartParser])
def generate_poster(request):
    description, style, poster_format, reference_file = _read_inputs(request)

    err = _validate(description, style, poster_format)
    if err is not None:
        return err

    if reference_file is not None:
        try:
            validate_reference_image(reference_file)
        except ReferenceImageValidationError as v_exc:
            return _error_json(v_exc.code, v_exc.message)

    description_clean = description.strip()
    prompt_global = build_poster_prompt(
        description_clean,
        style=style,
        format=poster_format,
        reference_present=reference_file is not None,
    )

    logger.info(
        "Poster generation request: style=%s format=%s desc_len=%s has_reference=%s",
        style, poster_format, len(description_clean), reference_file is not None,
    )
    logger.info("Prompt gen poster: %s", prompt_global)

    try:
        image = create_image_from_string(prompt_global, poster_format=poster_format)
    except ImageProviderError as exc:
        logger.error(
            "Poster generation provider error: code=%s provider_status=%s",
            exc.code, exc.provider_status,
        )
        return _provider_error_response(exc)

    # ``img2response`` returns an ``HttpResponse`` whose body is the base64
    # PNG bytes (legacy contract — the FE expects ``response.data`` to be
    # exactly that string). We don't wrap that response in JSON here so the
    # current FE keeps working unchanged.
    return img2response(image)


@api_view(['POST'])
@parser_classes([JSONParser, FormParser, MultiPartParser])
def edite_generative_poster(request):
    try:
        params = request.data['data']
    except (KeyError, TypeError):
        return _error_json(
            "POSTER_EDIT_PAYLOAD_INVALID",
            "Не удалось прочитать тело запроса.",
        )

    desc = params.get('correction', '') if isinstance(params, dict) else ''
    img_url = params.get('image', '') if isinstance(params, dict) else ''
    if not desc or not img_url:
        return _error_json(
            "POSTER_EDIT_PAYLOAD_INVALID",
            "Поля correction и image обязательны.",
        )

    prompt_global = (
        "Edit this input image according to the following text description, "
        "while preserving the overall look and feel of the original image: "
        f"{desc}"
    )
    logger.info("Prompt edit generative poster: %s", prompt_global)

    # Strip a possible ``data:image/...;base64,`` prefix and remember the
    # mime type so Gemini gets it right when we hand the bytes back in.
    mime_type = "image/png"
    if img_url.startswith("data:"):
        try:
            header, b64_payload = img_url.split(",", 1)
            mime_type = header.split(";")[0].removeprefix("data:") or mime_type
            img_url = b64_payload
        except ValueError:
            pass

    try:
        img_bytes = base64.b64decode(img_url)
    except (ValueError, base64.binascii.Error):
        return _error_json(
            "POSTER_EDIT_INVALID_IMAGE",
            "Не удалось декодировать изображение для правки.",
        )

    try:
        edited_bytes = edit_image_via_gemini(
            img_bytes, prompt_global, mime_type=mime_type
        )
    except GeminiImageError as exc:
        return _provider_error_response(_gemini_kind_to_provider_error(exc))

    logger.info("Image was edited.")
    # ``img2response`` accepts the legacy ``{"b64_json": ...}`` shape and
    # returns the same base64-PNG ``HttpResponse`` the FE already consumes.
    return img2response({"b64_json": base64.b64encode(edited_bytes).decode("ascii")})
