"""Non-persistent continuity-reference suggestions for storyboard keyframes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict


class ContinuitySuggestion(TypedDict):
    """A lightweight source keyframe suggestion for the API layer."""

    type: str
    keyframe_id: str
    reason: str


class ContinuityReferenceService:
    """Suggest the closest semantic continuity source without persisting it."""

    @classmethod
    def suggest(cls, keyframe: object) -> list[ContinuitySuggestion]:
        """Return zero or one primary continuity suggestion for ``keyframe``."""

        frame_type = cls._frame_type(keyframe)
        shot = getattr(keyframe, "shot", None)
        if shot is None:
            return []

        if frame_type == "start":
            return cls._suggest_previous_shot_end(shot)
        if frame_type == "intermediate":
            source = cls._preceding_keyframe(shot, keyframe)
            return cls._result(
                source,
                suggestion_type="previous_keyframe",
                reason="Previous keyframe in current shot",
            )
        if frame_type == "end":
            source, reason = cls._end_source(shot)
            return cls._result(
                source,
                suggestion_type="previous_keyframe",
                reason=reason,
            )
        return []

    @classmethod
    def _suggest_previous_shot_end(
        cls,
        shot: object,
    ) -> list[ContinuitySuggestion]:
        storyboard = getattr(shot, "storyboard", None)
        if storyboard is None:
            return []

        current_order = getattr(shot, "order", None)
        previous_shots = [
            candidate
            for candidate in cls._items(getattr(storyboard, "shots", ()))
            if getattr(candidate, "order", None) is not None
            and current_order is not None
            and candidate.order < current_order
        ]
        if not previous_shots:
            return []

        previous_shot = max(previous_shots, key=lambda candidate: candidate.order)
        previous_end = next(
            (
                candidate
                for candidate in cls._ordered_keyframes(previous_shot)
                if cls._frame_type(candidate) == "end"
            ),
            None,
        )
        return cls._result(
            previous_end,
            suggestion_type="previous_shot",
            reason="Previous shot end frame",
        )

    @classmethod
    def _preceding_keyframe(cls, shot: object, keyframe: object) -> object | None:
        ordered = cls._ordered_keyframes(shot)
        current_id = cls._identifier(keyframe)
        for index, candidate in enumerate(ordered):
            if cls._identifier(candidate) == current_id:
                return ordered[index - 1] if index > 0 else None

        current_position = getattr(keyframe, "position", None)
        preceding = [
            candidate
            for candidate in ordered
            if current_position is not None
            and getattr(candidate, "position", None) is not None
            and candidate.position < current_position
        ]
        return preceding[-1] if preceding else None

    @classmethod
    def _end_source(cls, shot: object) -> tuple[object | None, str]:
        ordered = cls._ordered_keyframes(shot)
        intermediates = [
            candidate
            for candidate in ordered
            if cls._frame_type(candidate) == "intermediate"
        ]
        if intermediates:
            return intermediates[-1], "Last intermediate frame in current shot"

        start = next(
            (
                candidate
                for candidate in ordered
                if cls._frame_type(candidate) == "start"
            ),
            None,
        )
        return start, "Shot start frame"

    @classmethod
    def _ordered_keyframes(cls, shot: object) -> list[object]:
        return sorted(
            cls._items(getattr(shot, "keyframes", ())),
            key=lambda keyframe: (
                getattr(keyframe, "position", 0),
                cls._identifier(keyframe),
            ),
        )

    @classmethod
    def _result(
        cls,
        source: object | None,
        *,
        suggestion_type: str,
        reason: str,
    ) -> list[ContinuitySuggestion]:
        if source is None:
            return []
        return [
            {
                "type": suggestion_type,
                "keyframe_id": cls._identifier(source),
                "reason": reason,
            }
        ]

    @staticmethod
    def _items(related: object) -> list[object]:
        if hasattr(related, "all"):
            return list(related.all())
        if isinstance(related, Iterable) and not isinstance(related, (str, bytes)):
            return list(related)
        return []

    @staticmethod
    def _identifier(instance: object) -> str:
        value = getattr(instance, "pk", None)
        if value is None:
            value = getattr(instance, "id", "")
        return str(value)

    @staticmethod
    def _normalized_value(value: object) -> str:
        raw_value = getattr(value, "value", value)
        return str(raw_value or "").strip().lower()

    @classmethod
    def _frame_type(cls, keyframe: object) -> str:
        return cls._normalized_value(getattr(keyframe, "type", ""))
