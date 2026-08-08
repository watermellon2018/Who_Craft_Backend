"""Reference-image provider resolution and deterministic Mock MVP provider."""

from __future__ import annotations

import hashlib
import io
from typing import Any

from django.conf import settings
from PIL import Image, ImageDraw, ImageEnhance

from w_craft_back.movie.reference_library.errors import ReferenceError
from w_craft_back.services.image_generation import resolve_provider_for_user
from w_craft_back.services.image_generation.base import ImageProvider
from w_craft_back.services.image_generation.resolver import resolve_current_for_user


class DeterministicReferenceMockProvider:
    """Produce stable valid PNGs without API keys or network calls."""

    name = "mock"
    model_id = "reference-mock-v1"

    @staticmethod
    def _dimensions(aspect_ratio: str | None) -> tuple[int, int]:
        return {
            "1:1": (512, 512),
            "4:3": (640, 480),
            "3:2": (672, 448),
            "16:9": (640, 360),
            "2:3": (448, 672),
        }.get(aspect_ratio or "1:1", (512, 512))

    @staticmethod
    def _png(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str | None = None,
        variant_count: int = 1,
        **kwargs: Any,
    ) -> list[bytes]:
        width, height = self._dimensions(aspect_ratio)
        variants: list[bytes] = []
        for index in range(variant_count):
            digest = hashlib.sha256(f"{prompt}|{index}".encode("utf-8")).digest()
            background = tuple(24 + value % 88 for value in digest[:3])
            accent = tuple(120 + value % 120 for value in digest[3:6])
            image = Image.new("RGB", (width, height), background)
            draw = ImageDraw.Draw(image)
            margin = max(20, min(width, height) // 10)
            for layer in range(5):
                inset = margin + layer * max(8, margin // 3)
                color = tuple((component + layer * 17) % 256 for component in accent)
                draw.rounded_rectangle(
                    (inset, inset, width - inset, height - inset),
                    radius=max(8, margin // 2),
                    outline=color,
                    width=max(2, margin // 12),
                )
            draw.ellipse(
                (
                    width // 2 - margin,
                    height // 2 - margin,
                    width // 2 + margin,
                    height // 2 + margin,
                ),
                fill=accent,
            )
            variants.append(self._png(image))
        return variants

    def edit(
        self,
        image_bytes: bytes,
        instruction: str,
        *,
        mime_type: str = "image/png",
        **kwargs: Any,
    ) -> bytes:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
        digest = hashlib.sha256(instruction.encode("utf-8")).digest()
        colorized = ImageEnhance.Color(image).enhance(0.8 + digest[0] / 512)
        overlay = Image.new("RGB", colorized.size, tuple(digest[:3]))
        edited = Image.blend(colorized, overlay, 0.08)
        return self._png(edited)

    def supports_edit(self) -> bool:
        return True


def provider_mode() -> str:
    """Return the explicit provider mode, defaulting to deterministic mock."""

    mode = str(
        getattr(settings, "REFERENCE_IMAGE_PROVIDER", "mock") or "mock"
    ).strip().lower()
    if mode not in {"mock", "registry"}:
        raise ReferenceError(
            "Reference image provider mode is invalid.",
            code="IMAGE_PROVIDER_NOT_CONFIGURED",
            http_status=503,
            retryable=True,
        )
    return mode


def effective_reference_model_key(
    *,
    actor: Any,
    project: Any,
    requested_model: str = "",
) -> str:
    """Return the registry key that must stay pinned to a durable job."""

    if provider_mode() == "mock":
        return DeterministicReferenceMockProvider.model_id
    project_model = str(
        (getattr(project, "generation_settings", {}) or {}).get(
            "image_generation_model",
            "",
        )
        or ""
    ).strip()
    explicit = requested_model.strip() or project_model
    if explicit:
        return explicit
    return str(resolve_current_for_user(actor).get("key") or "").strip()


def resolve_reference_provider(
    *,
    actor: Any,
    project: Any,
    requested_model: str = "",
    require_edit: bool = False,
) -> ImageProvider:
    """Resolve mock or registry provider without silent fallback."""

    if provider_mode() == "mock":
        environment = str(getattr(settings, "ENVIRONMENT", "development")).lower()
        if environment == "production" and not bool(
            getattr(settings, "REFERENCE_ALLOW_MOCK", False)
        ):
            raise ReferenceError(
                "Mock reference provider is disabled in production.",
                code="IMAGE_PROVIDER_NOT_CONFIGURED",
                http_status=503,
                retryable=True,
            )
        return DeterministicReferenceMockProvider()

    override = effective_reference_model_key(
        actor=actor,
        project=project,
        requested_model=requested_model,
    )
    return resolve_provider_for_user(
        actor,
        override=override or None,
        require_edit=require_edit,
    )


def resolve_pinned_reference_provider(
    *,
    actor: Any,
    requested_model: str,
    require_edit: bool = False,
) -> ImageProvider:
    """Resolve a durable registry job without consulting mutable mode settings."""

    return resolve_provider_for_user(
        actor,
        override=requested_model,
        require_edit=require_edit,
    )
