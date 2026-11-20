"""Provider-neutral values. Only operation IDs may be persisted by workers."""

from dataclasses import dataclass, field
from typing import Any, Literal


class VideoProviderError(Exception):
    """Safe, bounded error metadata; never retain provider bodies or credentials."""

    def __init__(
        self, code: str, message: str = '', *, retryable: bool = False,
        outcome_unknown: bool = False, safe_to_fallback: bool = False,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.safe_to_fallback = safe_to_fallback


@dataclass(frozen=True)
class VideoModelSpec:
    key: str
    provider: str
    provider_model: str
    label: str
    durations: tuple[int, ...]
    aspect_ratios: tuple[str, ...]
    resolutions: tuple[str, ...]
    supports_start_frame: bool = False
    supports_end_frame: bool = False
    supports_references: bool = False
    supports_audio: bool = False
    audio_always_on: bool = False
    # References and endpoint frames are distinct modes unless explicitly known.
    supports_references_with_frames: bool = False
    max_references: int = 0
    high_resolution_duration: int | None = None

    def public_data(self) -> dict[str, Any]:
        return {
            'key': self.key, 'provider': self.provider,
            'providerModel': self.provider_model, 'label': self.label,
            'durations': list(self.durations),
            'aspectRatios': list(self.aspect_ratios),
            'resolutions': list(self.resolutions),
            'supportsStartFrame': self.supports_start_frame,
            'supportsEndFrame': self.supports_end_frame,
            'supportsReferences': self.supports_references,
            'supportsReferencesWithFrames': self.supports_references_with_frames,
            'maxReferences': self.max_references,
            'supportsAudio': self.supports_audio,
            'audioAlwaysOn': self.audio_always_on,
        }


@dataclass(frozen=True)
class VideoInputImage:
    data: bytes = field(repr=False)
    content_type: str = 'image/png'


@dataclass(frozen=True)
class VideoGenerationRequest:
    model: VideoModelSpec
    prompt: str = field(repr=False)
    duration: int
    aspect_ratio: str = '16:9'
    resolution: str = '720p'
    start_frame: VideoInputImage | None = None
    end_frame: VideoInputImage | None = None
    reference_images: tuple[VideoInputImage, ...] = ()
    generate_audio: bool = True


@dataclass(frozen=True)
class VideoOperation:
    id: str
    provider: str
    model_key: str


@dataclass(frozen=True)
class VideoPollResult:
    status: Literal['processing', 'succeeded', 'failed']
    operation: VideoOperation
    # Ephemeral; never persist provider URLs, which may contain signed grants.
    download_url: str = field(default='', repr=False)
    error_code: str = ''


@dataclass(frozen=True)
class VideoDownload:
    data: bytes = field(repr=False)
    content_type: str = 'video/mp4'
    metadata: dict[str, Any] = field(default_factory=dict)
