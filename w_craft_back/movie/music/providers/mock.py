"""Deterministic, credential-free provider that emits playable PCM WAV."""

from __future__ import annotations

import hashlib
import io
import json
import math
import struct
import wave
from typing import Any, Mapping

from .base import (
    AudioProvider,
    AudioProviderCapabilities,
    ExecutionContextProtocol,
    GeneratedAudio,
    ProviderSubmission,
)


_SAMPLE_RATE = 8000
_AMPLITUDE = 9000


def _request_seed(request: Mapping[str, Any]) -> int:
    configured = request.get("baseSeed")
    if configured is not None:
        return int(configured)
    encoded = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:4], "big")


def _intent_frequency(request: Mapping[str, Any], seed: int, index: int) -> int:
    if request.get("referenceAssetId"):
        base = 523
    elif request.get("contentMode") == "song":
        base = 392
    else:
        base = 261
    return base + ((seed + index * 37) % 97)


def _wav_bytes(duration_seconds: float, frequency: int, seed: int) -> bytes:
    frame_count = max(1, round(duration_seconds * _SAMPLE_RATE))
    phase = (seed % 360) * math.pi / 180
    frames = bytearray(frame_count * 2)
    for frame_index in range(frame_count):
        value = int(
            _AMPLITUDE
            * math.sin(
                (2 * math.pi * frequency * frame_index / _SAMPLE_RATE) + phase
            )
        )
        struct.pack_into("<h", frames, frame_index * 2, value)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(_SAMPLE_RATE)
        wav_file.writeframes(frames)
    return output.getvalue()


class MockAudioProvider(AudioProvider):
    """Local deterministic provider covering instrumental/song/reference intents."""

    name = "mock"
    model_name = "deterministic-wav-v1"

    def capabilities(self) -> AudioProviderCapabilities:
        return AudioProviderCapabilities(
            provider_name=self.name,
            provider_display_name="Music generator",
            model_name=self.model_name,
        )

    def prepare_reference(
        self,
        stream,
        context: ExecutionContextProtocol,
    ) -> str:
        context.checkpoint()
        digest = hashlib.sha256()
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            context.checkpoint()
        return f"mock-reference:{digest.hexdigest()[:24]}"

    def submit(
        self,
        request: Mapping[str, Any],
        context: ExecutionContextProtocol,
    ) -> ProviderSubmission:
        context.checkpoint()
        duration = float(request["durationSeconds"])
        count = int(request["variantCount"])
        base_seed = _request_seed(request)
        outputs: list[GeneratedAudio] = []
        for index in range(count):
            context.checkpoint()
            seed = base_seed + index
            frequency = _intent_frequency(request, seed, index)
            payload = _wav_bytes(duration, frequency, seed)
            intent = (
                "reference"
                if request.get("referenceAssetId")
                else str(request.get("contentMode") or "instrumental")
            )
            outputs.append(
                GeneratedAudio(
                    payload=payload,
                    mime_type="audio/wav",
                    duration_seconds=duration,
                    seed=seed,
                    provider_request_id=f"mock:{base_seed}:{index}",
                    provenance={
                        "provider": self.name,
                        "model": self.model_name,
                        "watermark": False,
                    },
                    result_snapshot={
                        "intent": intent,
                        "lyricsLanguage": request.get("lyricsLanguage"),
                        "lyricsSections": request.get("lyricsSections", []),
                        "referenceAssetId": request.get("referenceAssetId"),
                        "frequencyHz": frequency,
                    },
                )
            )
        context.checkpoint()
        return ProviderSubmission(outputs=tuple(outputs))
