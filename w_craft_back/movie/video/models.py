"""Immutable conditions and recoverable external operations for video clips."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class VideoJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    OUTCOME_UNKNOWN = "outcome_unknown", "Outcome unknown"


class VideoGenerationJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("Project", on_delete=models.CASCADE,
                                related_name="video_generation_jobs")
    video_shot = models.ForeignKey("VideoShot", on_delete=models.RESTRICT,
                                   related_name="generation_jobs")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                              null=True, related_name="video_generation_jobs")
    client_request_id = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    source_fingerprint = models.CharField(max_length=64)
    source_snapshot = models.JSONField(default=dict)
    input_snapshot = models.JSONField(default=dict)
    provider_snapshot = models.JSONField(default=dict)
    status = models.CharField(max_length=24, choices=VideoJobStatus.choices,
                              default=VideoJobStatus.QUEUED)
    stage = models.CharField(max_length=24, default="queued")
    operation_id = models.CharField(max_length=512, blank=True, default="")
    provider_started_at = models.DateTimeField(null=True, blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    poll_failures = models.PositiveIntegerField(default=0)
    execution_candidate_index = models.PositiveSmallIntegerField(default=0)
    result_asset = models.OneToOneField(
        "ProjectAsset", on_delete=models.RESTRICT, null=True, blank=True,
        related_name="generated_video_job")
    error_code = models.CharField(max_length=128, blank=True, default="")
    error_detail = models.CharField(max_length=500, blank=True, default="")
    error_retryable = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(
            fields=["project", "actor", "client_request_id"],
            name="uniq_video_job_request",
        )]
        indexes = [models.Index(fields=["status", "next_poll_at"]),
                   models.Index(fields=["project", "-created_at"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values(
                "source_fingerprint", "source_snapshot", "input_snapshot",
                "provider_snapshot", "project_id", "video_shot_id",
                "request_fingerprint", "client_request_id",
            ).first()
            if original and any(getattr(self, key) != value
                                for key, value in original.items()):
                raise ValidationError("Video generation inputs are immutable.")
        if self.video_shot.project_id != self.project_id:
            raise ValidationError("Video shot must belong to the job project.")
        if self.result_asset_id and self.result_asset.project_id != self.project_id:
            raise ValidationError("Video result must belong to the job project.")
        return super().save(*args, **kwargs)


class VideoGenerationInputAsset(models.Model):
    """Protect every accepted input from deletion while preserving provenance."""
    job = models.ForeignKey(VideoGenerationJob, on_delete=models.CASCADE,
                            related_name="input_assets")
    asset = models.ForeignKey("ProjectAsset", on_delete=models.RESTRICT,
                              related_name="video_generation_inputs")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "asset"],
                                               name="uniq_video_input_asset")]
