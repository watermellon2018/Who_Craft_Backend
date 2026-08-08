"""Provider-neutral protocol for bounded audio generation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Mapping, Protocol


class ExecutionContextProtocol(Protocol):
    """Lifecycle-owned cancellation and lease checkpoint exposed to providers."""

    def heartbeat(self) -> None:
        """Renew the lease or raise when its fence was lost."""

    def is_cancelled(self) -> bool:
        """Return whether durable cancellation has been requested."""

    def checkpoint(self) -> None:
        """Check cancellation and renew the owned lease."""


class MusicProviderError(RuntimeError):
    """Adapter failure whose raw message must never be exposed publicly."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "MUSIC_PROVIDER_UNAVAILABLE",
        http_status: int = 503,
        retryable: bool = True,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class AudioProviderCapabilities:
    """Effective feature/limit profile advertised by an audio provider."""

    provider_name: str
    provider_display_name: str
    model_name: str
    content_modes: tuple[str, ...] = ("instrumental", "song")
    variant_counts: tuple[int, ...] = (1, 2)
    min_duration_seconds: int = 3
    max_duration_seconds: int = 300
    output_formats: tuple[str, ...] = ("wav",)
    lyrics_languages: tuple[str, ...] = ("ru", "en")
    lyrics_section_types: tuple[str, ...] = (
        "verse",
        "chorus",
        "bridge",
        "outro",
    )
    max_lyrics_chars: int = 12000
    supports_audio_reference: bool = True
    reference_formats: tuple[str, ...] = ("mp3", "wav", "ogg")
    max_reference_bytes: int = 50 * 1024 * 1024
    min_reference_seconds: int = 10
    max_reference_seconds: int = 300
    supports_seed: bool = True
    supports_cancellation: bool = False
    supports_external_async: bool = False

    def as_public_dict(self) -> dict[str, Any]:
        """Return the stable camelCase capabilities contract consumed by the API."""

        return {
            "contentModes": list(self.content_modes),
            "variantCounts": list(self.variant_counts),
            "duration": {
                "minSeconds": self.min_duration_seconds,
                "maxSeconds": self.max_duration_seconds,
                "defaultSeconds": 30,
            },
            "outputFormats": list(self.output_formats),
            "lyrics": {
                "supported": "song" in self.content_modes,
                "languages": list(self.lyrics_languages),
                "maxChars": self.max_lyrics_chars,
                "sectionTypes": list(self.lyrics_section_types),
            },
            "audioReference": {
                "supported": self.supports_audio_reference,
                "maxCount": 1,
                "formats": list(self.reference_formats),
                "maxBytes": self.max_reference_bytes,
                "minSeconds": self.min_reference_seconds,
                "maxSeconds": self.max_reference_seconds,
            },
            "supportsSeed": self.supports_seed,
            "supportsCancellation": self.supports_cancellation,
            "providerDisplayName": self.provider_display_name,
        }


@dataclass(frozen=True)
class GeneratedAudio:
    """One provider output; raw bytes never enter the database."""

    payload: bytes
    mime_type: str
    duration_seconds: float
    seed: int | None = None
    provider_request_id: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict)
    result_snapshot: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderSubmission:
    """Immediate outputs or a durable external handle, never both."""

    outputs: tuple[GeneratedAudio, ...] = ()
    external_job_id: str = ""
    poll_after_seconds: float | None = None
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if bool(self.outputs) == bool(self.external_job_id):
            raise ValueError(
                "Provider submission requires outputs or an external job id."
            )


class AudioProvider(ABC):
    """Provider adapter isolated from ORM and public HTTP serialization."""

    name: str
    model_name: str

    @abstractmethod
    def capabilities(self) -> AudioProviderCapabilities:
        """Return the effective provider capability profile."""

    def prepare_reference(
        self,
        stream: BinaryIO,
        context: ExecutionContextProtocol,
    ) -> str:
        """Optionally prepare a private reference and return an opaque handle."""

        del stream
        context.checkpoint()
        return ""

    @abstractmethod
    def submit(
        self,
        request: Mapping[str, Any],
        context: ExecutionContextProtocol,
    ) -> ProviderSubmission:
        """Submit one request, cooperatively checkpointing blocking work."""

    def poll(
        self,
        external_job_id: str,
        context: ExecutionContextProtocol,
    ) -> ProviderSubmission:
        """Poll an external provider job when async execution is supported."""

        del external_job_id
        context.checkpoint()
        raise MusicProviderError(
            "The selected provider does not support asynchronous polling.",
            code="MUSIC_CAPABILITY_UNSUPPORTED",
            http_status=400,
            retryable=False,
        )

    def cancel(
        self,
        external_job_id: str,
        context: ExecutionContextProtocol,
    ) -> bool:
        """Best-effort cancellation for providers that expose it."""

        del external_job_id
        context.checkpoint()
        return False
