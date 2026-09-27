"""Settle definitely unsubmitted work when its project aggregate is removed."""
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from w_craft_back.credits.video import release_video_generation

from .models import VideoGenerationJob


@receiver(pre_delete, sender=VideoGenerationJob)
def settle_deleted_video_job(sender, instance, **kwargs) -> None:
    current = VideoGenerationJob.objects.select_for_update().filter(
        pk=instance.pk,
    ).first() or instance
    if (not current.provider_started_at and not current.operation_id
            and current.status != "outcome_unknown"):
        release_video_generation(current, reason="video_job_deleted_before_submit")
