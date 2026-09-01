"""Infer semantic camera movement between adjacent storyboard keyframes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict


class MovementResolution(TypedDict):
    """JSON-ready result of a camera movement inference."""

    movement: str
    metadata: dict[str, Any]


class CameraMovementResolver:
    """Classify the primary CameraIntent change without rejecting ambiguity."""

    AZIMUTH_ORDER = (
        "front",
        "front_right",
        "right",
        "back_right",
        "back",
        "back_left",
        "left",
        "front_left",
    )
    DISTANCE_ORDER = ("wide", "medium", "near")
    ELEVATION_ORDER = ("low", "eye_level", "high", "top")
    PRIMARY_FIELDS = ("azimuth", "distance", "elevation", "target")

    @classmethod
    def resolve(cls, from_intent: object, to_intent: object) -> MovementResolution:
        """Return the inferred movement and diagnostic metadata.

        Only the four fields that describe a camera's primary spatial change are
        used for classification. Framing, lens, composition, and provider metadata
        may change without implying physical camera movement.
        """

        changes = sorted(
            field
            for field in cls.PRIMARY_FIELDS
            if cls._primary_value(from_intent, field)
            != cls._primary_value(to_intent, field)
        )
        metadata: dict[str, Any] = {"changes": changes}

        if not changes:
            return {"movement": "static", "metadata": metadata}

        if len(changes) > 1:
            return {"movement": "custom", "metadata": metadata}

        changed_field = changes[0]
        if changed_field == "distance":
            return cls._resolve_ordered_change(
                from_intent,
                to_intent,
                field="distance",
                order=cls.DISTANCE_ORDER,
                increasing="dolly_in",
                decreasing="dolly_out",
                metadata=metadata,
            )
        if changed_field == "azimuth":
            return cls._resolve_orbit(from_intent, to_intent, metadata)
        if changed_field == "elevation":
            return cls._resolve_ordered_change(
                from_intent,
                to_intent,
                field="elevation",
                order=cls.ELEVATION_ORDER,
                increasing="crane_up",
                decreasing="crane_down",
                metadata=metadata,
            )
        if changed_field == "target":
            return cls._resolve_pan(from_intent, to_intent, metadata)

        return {"movement": "custom", "metadata": metadata}

    @classmethod
    def _resolve_ordered_change(
        cls,
        from_intent: object,
        to_intent: object,
        *,
        field: str,
        order: Sequence[str],
        increasing: str,
        decreasing: str,
        metadata: dict[str, Any],
    ) -> MovementResolution:
        from_value = cls._enum_value(cls._value(from_intent, field))
        to_value = cls._enum_value(cls._value(to_intent, field))
        try:
            from_index = order.index(from_value)
            to_index = order.index(to_value)
        except ValueError:
            metadata["reason"] = f"unknown_{field}"
            return {"movement": "custom", "metadata": metadata}

        movement = increasing if to_index > from_index else decreasing
        return {"movement": movement, "metadata": metadata}

    @classmethod
    def _resolve_orbit(
        cls,
        from_intent: object,
        to_intent: object,
        metadata: dict[str, Any],
    ) -> MovementResolution:
        from_value = cls._enum_value(cls._value(from_intent, "azimuth"))
        to_value = cls._enum_value(cls._value(to_intent, "azimuth"))
        try:
            from_index = cls.AZIMUTH_ORDER.index(from_value)
            to_index = cls.AZIMUTH_ORDER.index(to_value)
        except ValueError:
            metadata["reason"] = "unknown_azimuth"
            return {"movement": "custom", "metadata": metadata}

        clockwise_steps = (to_index - from_index) % len(cls.AZIMUTH_ORDER)
        movement = (
            "orbit_right"
            if clockwise_steps <= len(cls.AZIMUTH_ORDER) / 2
            else "orbit_left"
        )
        metadata["azimuth_steps"] = min(
            clockwise_steps,
            len(cls.AZIMUTH_ORDER) - clockwise_steps,
        )
        return {"movement": movement, "metadata": metadata}

    @classmethod
    def _resolve_pan(
        cls,
        from_intent: object,
        to_intent: object,
        metadata: dict[str, Any],
    ) -> MovementResolution:
        from_center = cls._target_center_x(from_intent)
        to_center = cls._target_center_x(to_intent)
        if from_center is None or to_center is None or from_center == to_center:
            metadata["reason"] = "insufficient_composition"
            return {"movement": "custom", "metadata": metadata}

        metadata.update(
            {
                "from_target_center_x": from_center,
                "to_target_center_x": to_center,
            }
        )
        movement = "pan_right" if to_center > from_center else "pan_left"
        return {"movement": movement, "metadata": metadata}

    @classmethod
    def _target_center_x(cls, intent: object) -> float | None:
        target_ids = cls._target_ids(cls._value(intent, "target"))
        if not target_ids:
            return None

        composition = cls._value(intent, "composition")
        if not isinstance(composition, Sequence) or isinstance(
            composition,
            (str, bytes),
        ):
            return None

        centers: list[float] = []
        for subject in composition:
            if not isinstance(subject, Mapping):
                continue
            subject_id = subject.get("subject_id")
            if subject_id is None or str(subject_id) not in target_ids:
                continue
            try:
                x = float(subject["x"])
                width = float(subject["width"])
            except (KeyError, TypeError, ValueError):
                continue
            centers.append(x + width / 2)

        if not centers:
            return None
        return sum(centers) / len(centers)

    @staticmethod
    def _target_ids(target: object) -> set[str]:
        if isinstance(target, Mapping):
            raw_ids = target.get("ids", ())
            if isinstance(raw_ids, Sequence) and not isinstance(
                raw_ids,
                (str, bytes),
            ):
                return {str(item) for item in raw_ids}
            raw_id = target.get("id")
            return {str(raw_id)} if raw_id is not None else set()
        if target is None:
            return set()
        return {str(target)}

    @classmethod
    def _primary_value(cls, intent: object, field: str) -> object:
        value = cls._value(intent, field)
        if field == "target":
            return cls._canonical_target(value)
        return cls._enum_value(value)

    @classmethod
    def _canonical_target(cls, target: object) -> object:
        if not isinstance(target, Mapping):
            return target
        return tuple(
            sorted(
                (
                    str(key),
                    tuple(sorted(str(item) for item in value))
                    if key == "ids"
                    and isinstance(value, Sequence)
                    and not isinstance(value, (str, bytes))
                    else cls._canonical_target(value),
                )
                for key, value in target.items()
            )
        )

    @staticmethod
    def _enum_value(value: object) -> str:
        raw_value = getattr(value, "value", value)
        return str(raw_value or "").strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _value(instance: object, field: str) -> object:
        if isinstance(instance, Mapping):
            return instance.get(field)
        return getattr(instance, field, None)
