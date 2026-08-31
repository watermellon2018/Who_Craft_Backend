"""Maintain camera transitions for the ordered keyframes of a shot."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypedDict

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from .movement import CameraMovementResolver


class AdjacentTransitions(TypedDict):
    """Transitions immediately before and after one keyframe."""

    from_previous: Any | None
    to_next: Any | None


def ordered_keyframes(shot_or_keyframes: object) -> list[object]:
    """Return keyframes ordered by their normalized timeline position."""

    related = getattr(shot_or_keyframes, "keyframes", shot_or_keyframes)
    if hasattr(related, "all"):
        items = list(related.all())
    elif isinstance(related, Iterable) and not isinstance(related, (str, bytes)):
        items = list(related)
    else:
        items = []
    return sorted(
        items,
        key=lambda keyframe: (
            getattr(keyframe, "position", 0),
            str(getattr(keyframe, "pk", getattr(keyframe, "id", ""))),
        ),
    )


@transaction.atomic
def rebuild_transitions(shot: object) -> list[object]:
    """Rebuild exactly the adjacent edges and retain overrides on surviving edges."""

    from w_craft_back.movie.storyboard.models import (
        CameraTransition,
        StoryboardKeyframe,
        StoryboardShot,
    )

    locked_shot = StoryboardShot.objects.select_for_update().get(pk=shot.pk)
    keyframes = list(
        StoryboardKeyframe.objects.select_for_update()
        .filter(shot=locked_shot)
        .order_by("position", "pk")
    )
    existing = {
        (transition.from_keyframe_id, transition.to_keyframe_id): transition
        for transition in CameraTransition.objects.select_for_update().filter(
            shot=locked_shot
        )
    }
    expected_keys = {
        (from_keyframe.pk, to_keyframe.pk)
        for from_keyframe, to_keyframe in zip(keyframes, keyframes[1:])
    }

    for edge, stale_transition in existing.items():
        if edge not in expected_keys:
            stale_transition.delete()

    rebuilt: list[object] = []
    for from_keyframe, to_keyframe in zip(keyframes, keyframes[1:]):
        edge = (from_keyframe.pk, to_keyframe.pk)
        transition = _upsert_transition(
            locked_shot,
            from_keyframe,
            to_keyframe,
            existing=existing.get(edge),
            transition_model=CameraTransition,
        )
        rebuilt.append(transition)
    return rebuilt


@transaction.atomic
def recalculate_adjacent_transitions(keyframe: object) -> AdjacentTransitions:
    """Recalculate only previous→current and current→next transition metadata."""

    from w_craft_back.movie.storyboard.models import (
        CameraTransition,
        StoryboardKeyframe,
        StoryboardShot,
    )

    shot_id = getattr(keyframe, "shot_id", None)
    locked_shot = StoryboardShot.objects.select_for_update().get(pk=shot_id)
    keyframes = list(
        StoryboardKeyframe.objects.select_for_update()
        .filter(shot=locked_shot)
        .order_by("position", "pk")
    )
    current_index = next(
        index
        for index, candidate in enumerate(keyframes)
        if candidate.pk == keyframe.pk
    )

    previous_transition = None
    if current_index > 0:
        previous_transition = _recalculate_edge(
            locked_shot,
            keyframes[current_index - 1],
            keyframes[current_index],
            CameraTransition,
        )

    next_transition = None
    if current_index < len(keyframes) - 1:
        next_transition = _recalculate_edge(
            locked_shot,
            keyframes[current_index],
            keyframes[current_index + 1],
            CameraTransition,
        )

    return {
        "from_previous": previous_transition,
        "to_next": next_transition,
    }


def _recalculate_edge(
    shot: object,
    from_keyframe: object,
    to_keyframe: object,
    transition_model: object,
) -> object:
    existing = (
        transition_model.objects.select_for_update()
        .filter(
            shot=shot,
            from_keyframe=from_keyframe,
            to_keyframe=to_keyframe,
        )
        .first()
    )
    return _upsert_transition(
        shot,
        from_keyframe,
        to_keyframe,
        existing=existing,
        transition_model=transition_model,
    )


def _upsert_transition(
    shot: object,
    from_keyframe: object,
    to_keyframe: object,
    *,
    existing: object | None,
    transition_model: object,
) -> object:
    resolution = _resolve_keyframe_movement(from_keyframe, to_keyframe)
    if existing is None:
        return transition_model.objects.create(
            shot=shot,
            from_keyframe=from_keyframe,
            to_keyframe=to_keyframe,
            detected_movement=resolution["movement"],
            metadata=resolution["metadata"],
        )

    existing.detected_movement = resolution["movement"]
    existing.metadata = resolution["metadata"]
    existing.save(update_fields=["detected_movement", "metadata", "updated_at"])
    return existing


def _resolve_keyframe_movement(
    from_keyframe: object,
    to_keyframe: object,
) -> dict[str, Any]:
    from_intent = _camera_intent(from_keyframe)
    to_intent = _camera_intent(to_keyframe)
    if from_intent is None or to_intent is None:
        return {
            "movement": "custom",
            "metadata": {
                "changes": [],
                "reason": "missing_camera_intent",
            },
        }
    return CameraMovementResolver.resolve(from_intent, to_intent)


def _camera_intent(keyframe: object) -> object | None:
    try:
        return getattr(keyframe, "camera_intent", None)
    except ObjectDoesNotExist:
        return None
