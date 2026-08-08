"""Image-generation provider protocol."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ImageProvider(Protocol):
    """Anything that can produce or edit images on demand.

    Implementations must raise :class:`.errors.ImageProviderError` on any
    failure that should reach the API caller — translation from native
    SDK exceptions happens inside the provider, not in the views.
    """

    name: str
    model_id: str

    def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str | None = None,
        variant_count: int = 1,
        **kwargs: Any,
    ) -> list[bytes]:
        ...

    def edit(
        self,
        image_bytes: bytes,
        instruction: str,
        *,
        mime_type: str = "image/png",
        **kwargs: Any,
    ) -> bytes:
        ...

    def supports_edit(self) -> bool:
        ...
