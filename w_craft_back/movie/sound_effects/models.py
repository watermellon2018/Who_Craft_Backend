"""Durable sound-effect entities, immutable versions, jobs, and assignments."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from w_craft_back.movie.project.dashboard_models import Scene
from w_craft_back.movie.project.models import Project


class SoundEffectJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class SoundEffectJobStage(models.TextChoices):
    QUEUED = "queued", "Queued"
    GENERATING = "generating", "Generating"
    STORING = "storing", "Storing"
    FINALIZED = "finalized", "Finalized"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class SoundEffectAsset(models.Model):
    """One immutable generated audio object and its verified metadata."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="sound_effect_assets",
    )
    file = models.FileField(
        upload_to="projects/sound-effects/assets/",
        max_length=500,
    )
    mime_type = models.CharField(max_length=64)
    size_bytes = models.BigIntegerField()
    checksum_sha256 = models.CharField(max_length=64)
    duration_seconds = models.DecimalField(max_digits=8, decimal_places=3)
    provider = models.CharField(max_length=64)
    model_name = models.CharField(max_length=128)
    provider_request_id = models.CharField(max_length=255, blank=True, default="")
    provenance = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_sound_effect_assets",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["project", "-created_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(size_bytes__gt=0),
                name="chk_sfx_asset_positive_size",
            ),
            models.CheckConstraint(
                condition=models.Q(duration_seconds__gt=0),
                name="chk_sfx_asset_positive_duration",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding and self.pk:
            raise ValidationError("Sound-effect assets are immutable.")
        return super().save(*args, **kwargs)


class SoundEffect(models.Model):
    """Logical reusable effect whose active pointer may advance by version."""

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="sound_effects",
    )
    title = models.CharField(max_length=255)
    active_version = models.ForeignKey(
        "SoundEffectVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_for_effects",
    )
    version = models.PositiveIntegerField(default=1)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_sound_effects",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [models.Index(fields=["project", "archived_at"])]

    def clean(self) -> None:
        super().clean()
        if self.active_version_id and (
            not self.pk or self.active_version.effect_id != self.pk
        ):
            raise ValidationError(
                {"active_version": "Active version must belong to this effect."}
            )

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class SoundEffectGenerationJob(models.Model):
    """Durable request and immutable provider/pricing execution snapshot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="sound_effect_generation_jobs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sound_effect_generation_jobs",
    )
    target_effect = models.ForeignKey(
        SoundEffect,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generation_jobs",
    )
    target_scene = models.ForeignKey(
        Scene,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sound_effect_generation_jobs",
    )
    request = models.JSONField(default=dict)
    provider = models.CharField(max_length=64, default="elevenlabs-sfx")
    model_name = models.CharField(
        max_length=128,
        default="eleven_text_to_sound_v2",
    )
    provider_snapshot = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16,
        choices=SoundEffectJobStatus.choices,
        default=SoundEffectJobStatus.QUEUED,
    )
    stage = models.CharField(
        max_length=16,
        choices=SoundEffectJobStage.choices,
        default=SoundEffectJobStage.QUEUED,
    )
    idempotency_key = models.CharField(max_length=128)
    request_fingerprint = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    retry_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retries",
    )
    error_code = models.CharField(max_length=128, blank=True, default="")
    error_detail = models.CharField(max_length=500, blank=True, default="")
    error_http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error_retryable = models.BooleanField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "-created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "actor", "idempotency_key"],
                name="uniq_sfx_job_idempotency",
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__lte=models.F("max_attempts")),
                name="chk_sfx_job_attempts",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.target_effect_id and (
            self.target_effect.project_id != self.project_id
        ):
            raise ValidationError(
                {"target_effect": "Target effect must belong to this project."}
            )
        if self.target_scene_id and self.target_scene.project_id != self.project_id:
            raise ValidationError(
                {"target_scene": "Target scene must belong to this project."}
            )
        if self.retry_of_id and self.retry_of.project_id != self.project_id:
            raise ValidationError(
                {"retry_of": "Retry source must belong to this project."}
            )

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class SoundEffectVariant(models.Model):
    """The single generated candidate belonging to a sound-effect job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.OneToOneField(
        SoundEffectGenerationJob,
        on_delete=models.CASCADE,
        related_name="variant",
    )
    asset = models.OneToOneField(
        SoundEffectAsset,
        on_delete=models.PROTECT,
        related_name="generated_variant",
    )
    provider_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self) -> None:
        super().clean()
        if self.job_id and self.asset_id:
            if self.job.project_id != self.asset.project_id:
                raise ValidationError(
                    {"asset": "Variant asset must belong to the job project."}
                )

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class SoundEffectVersion(models.Model):
    """Immutable accepted version of a logical sound effect."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    effect = models.ForeignKey(
        SoundEffect,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    asset = models.ForeignKey(
        SoundEffectAsset,
        on_delete=models.PROTECT,
        related_name="effect_versions",
    )
    request_snapshot = models.JSONField(default=dict)
    source_variant = models.OneToOneField(
        SoundEffectVariant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_version",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_sound_effect_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["effect", "version_number"],
                name="uniq_sfx_effect_version_number",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.effect_id and self.asset_id:
            if self.effect.project_id != self.asset.project_id:
                raise ValidationError(
                    {"asset": "Version asset must belong to the effect project."}
                )
        if self.source_variant_id and (
            self.source_variant.job.project_id != self.effect.project_id
        ):
            raise ValidationError(
                {"source_variant": "Source variant belongs to another project."}
            )

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise ValidationError("Sound-effect versions are immutable.")
        self.clean()
        return super().save(*args, **kwargs)


class SceneSoundEffect(models.Model):
    """Project-safe placement of one immutable effect version on a scene."""

    scene = models.ForeignKey(
        Scene,
        on_delete=models.CASCADE,
        related_name="sound_effect_assignments",
    )
    effect = models.ForeignKey(
        SoundEffect,
        on_delete=models.CASCADE,
        related_name="scene_assignments",
    )
    effect_version = models.ForeignKey(
        SoundEffectVersion,
        on_delete=models.PROTECT,
        related_name="scene_assignments",
    )
    start_time_seconds = models.DecimalField(max_digits=10, decimal_places=3)

    class Meta:
        ordering = ["scene_id", "start_time_seconds", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["scene", "effect", "start_time_seconds"],
                name="uniq_scene_sfx_position",
            ),
            models.CheckConstraint(
                condition=models.Q(start_time_seconds__gte=0),
                name="chk_scene_sfx_nonnegative_start",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.scene_id and self.effect_id:
            if self.scene.project_id != self.effect.project_id:
                raise ValidationError(
                    {"effect": "Effect must belong to the scene project."}
                )
        if self.effect_version_id and (
            self.effect_version.effect_id != self.effect_id
        ):
            raise ValidationError(
                {"effect_version": "Version must belong to this effect."}
            )

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)
