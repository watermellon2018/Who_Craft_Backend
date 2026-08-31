"""Settlement and media cleanup for Storyboard generation deletion cascades."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import RestrictedError
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from w_craft_back.movie.project.dashboard_models import ProjectAsset
from w_craft_back.movie.storyboard.lifecycle import (
    settle_failed_storyboard_generation,
)
from w_craft_back.movie.storyboard.models import (
    StoryboardKeyframe,
    StoryboardKeyframeGeneration,
    StoryboardShot,
)


logger = logging.getLogger(__name__)


@receiver(pre_delete, sender=StoryboardKeyframeGeneration)
def release_deleted_storyboard_generation(sender, instance, **kwargs) -> None:
    """Settle queued/cascaded jobs in the same transaction as their deletion."""

    # Cascade collectors may have loaded the instance before a worker claim.
    # Lock and refresh it so the settlement reflects whether a provider call
    # actually started, while queued work cannot be claimed during deletion.
    ownership = StoryboardKeyframeGeneration.objects.filter(
        pk=instance.pk,
    ).values("keyframe__shot_id").first()
    if ownership is not None:
        shot = StoryboardShot.objects.select_for_update().filter(
            pk=ownership["keyframe__shot_id"],
        ).first()
        if shot is not None:
            list(
                StoryboardKeyframe.objects.select_for_update()
                .filter(shot=shot)
                .order_by("pk")
            )
    current = (
        StoryboardKeyframeGeneration.objects.select_for_update()
        .filter(pk=instance.pk)
        .first()
    )
    settle_failed_storyboard_generation(
        current or instance,
        reason="storyboard_generation_deleted",
        outcome_unknown=bool(
            (current or instance).status == "generating"
            and (current or instance).provider_started_at is not None
        ),
    )


@receiver(post_delete, sender=StoryboardKeyframeGeneration)
def delete_generated_storyboard_asset(sender, instance, **kwargs) -> None:
    """Remove a generation-owned ProjectAsset after the deletion commits."""

    asset_id = instance.asset_id
    if asset_id is None:
        return

    def cleanup() -> None:
        try:
            ProjectAsset.objects.filter(pk=asset_id).delete()
        except RestrictedError:
            # Another aggregate intentionally owns the same asset.
            logger.info(
                "Storyboard asset retained because it is still referenced: %s",
                asset_id,
            )

    transaction.on_commit(cleanup)
