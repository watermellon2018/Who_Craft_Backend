"""Computed readiness for storyboard shots and scene storyboards."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict

from django.core.exceptions import ObjectDoesNotExist


class ReadinessResult(TypedDict):
    """Serializable readiness result with stable missing-condition codes."""

    ready: bool
    missing: list[str]


class ShotReadinessService:
    """Evaluate the minimum required START and END keyframe state."""

    READY_STATUS = "ready"

    @classmethod
    def evaluate(cls, shot: object) -> ReadinessResult:
        """Compute readiness without mutating or caching it on the model."""

        keyframes = cls._items(getattr(shot, "keyframes", ()))
        start = cls._find_frame(keyframes, "start")
        end = cls._find_frame(keyframes, "end")
        missing: list[str] = []

        if start is None:
            missing.append("start_keyframe")
        else:
            cls._append_frame_missing(start, "start", missing)

        if end is None:
            missing.append("end_keyframe")
        else:
            cls._append_frame_missing(end, "end", missing)

        return {"ready": not missing, "missing": missing}

    @classmethod
    def storyboard_status(cls, storyboard: object) -> str:
        """Return ``empty``, ``draft``, or ``completed`` from current shots."""

        shots = cls._items(getattr(storyboard, "shots", ()))
        if not shots:
            return "empty"
        if all(cls.evaluate(shot)["ready"] for shot in shots):
            return "completed"
        return "draft"

    @classmethod
    def _append_frame_missing(
        cls,
        keyframe: object,
        prefix: str,
        missing: list[str],
    ) -> None:
        if cls._camera_intent(keyframe) is None:
            missing.append(f"{prefix}_camera_intent")
        if not cls._has_ready_generation(keyframe):
            missing.append(f"{prefix}_image")

    @staticmethod
    def _camera_intent(keyframe: object) -> object | None:
        try:
            return getattr(keyframe, "camera_intent", None)
        except ObjectDoesNotExist:
            return None

    @classmethod
    def _has_ready_generation(cls, keyframe: object) -> bool:
        generation = getattr(keyframe, "current_generation", None)
        if generation is None:
            return False
        raw_status = getattr(generation, "status", "")
        status = getattr(raw_status, "value", raw_status)
        return str(status).strip().lower() == cls.READY_STATUS

    @classmethod
    def _find_frame(
        cls,
        keyframes: list[object],
        frame_type: str,
    ) -> object | None:
        return next(
            (
                keyframe
                for keyframe in keyframes
                if cls._normalized_value(getattr(keyframe, "type", "")) == frame_type
            ),
            None,
        )

    @staticmethod
    def _items(related: object) -> list[object]:
        if hasattr(related, "all"):
            return list(related.all())
        if isinstance(related, Iterable) and not isinstance(related, (str, bytes)):
            return list(related)
        return []

    @staticmethod
    def _normalized_value(value: object) -> str:
        raw_value = getattr(value, "value", value)
        return str(raw_value or "").strip().lower()


def compute_storyboard_status(storyboard: object) -> str:
    """Convenience wrapper for callers that only need the aggregate status."""

    return ShotReadinessService.storyboard_status(storyboard)
