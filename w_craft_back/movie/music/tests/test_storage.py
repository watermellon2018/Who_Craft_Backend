from __future__ import annotations

import io
import struct
import wave

from django.test import SimpleTestCase

from w_craft_back.storage_gateway import (
    InvalidAudio,
    UnsupportedMedia,
    normalize_audio_bytes,
)


def wav_bytes(seconds: float = 0.1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b"\0\0" * round(8000 * seconds))
    return output.getvalue()


def mp3_bytes(frame_count: int = 10) -> bytes:
    header = b"\xff\xfb\x90\x00"  # MPEG1 Layer III, 128 kbps, 44.1 kHz.
    frame_length = 417
    return b"".join(header + bytes(frame_length - 4) for _ in range(frame_count))


def ogg_page(body: bytes, *, sequence: int, granule: int, flags: int) -> bytes:
    lacing = bytes([len(body)])
    return (
        b"OggS"
        + b"\0"
        + bytes([flags])
        + struct.pack("<Q", granule)
        + struct.pack("<I", 12)
        + struct.pack("<I", sequence)
        + b"\0\0\0\0"
        + bytes([len(lacing)])
        + lacing
        + body
    )


def ogg_bytes() -> bytes:
    identification = (
        b"\x01vorbis"
        + struct.pack("<I", 0)
        + b"\x01"
        + struct.pack("<I", 8000)
        + bytes(14)
    )
    return ogg_page(identification, sequence=0, granule=0, flags=2) + ogg_page(
        b"\0", sequence=1, granule=8000, flags=4
    )


class AudioStorageValidationTests(SimpleTestCase):
    def test_wav_mp3_and_ogg_are_structurally_probed(self):
        wav = normalize_audio_bytes(wav_bytes(), min_duration_seconds=0.01)
        mp3 = normalize_audio_bytes(mp3_bytes(), min_duration_seconds=0.01)
        ogg = normalize_audio_bytes(ogg_bytes(), min_duration_seconds=0.01)
        self.assertEqual(wav.mime_type, "audio/wav")
        self.assertEqual(mp3.mime_type, "audio/mpeg")
        self.assertEqual(ogg.mime_type, "audio/ogg")
        self.assertAlmostEqual(ogg.duration_seconds, 1.0)

    def test_spoofed_and_truncated_audio_are_rejected(self):
        with self.assertRaises(UnsupportedMedia):
            normalize_audio_bytes(b"not audio", min_duration_seconds=0.01)
        with self.assertRaises(InvalidAudio):
            normalize_audio_bytes(wav_bytes()[:-4], min_duration_seconds=0.01)
        with self.assertRaises(InvalidAudio):
            normalize_audio_bytes(mp3_bytes()[:-1], min_duration_seconds=0.01)
        with self.assertRaises(InvalidAudio):
            normalize_audio_bytes(ogg_bytes()[:-1], min_duration_seconds=0.01)
