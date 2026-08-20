"""Durable models for Music Studio assets, versions, jobs, and variants."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from w_craft_back.movie.project.dashboard_models import MusicTrack
from w_craft_back.movie.project.models import Project


class MusicAssetRole(models.TextChoices):
    GENERATED = "generated", "Generated audio"
    REFERENCE = "reference", "Private audio reference"


class MusicAssetOrigin(models.TextChoices):
    GENERATED = "generated", "Generated"
    UPLOAD = "upload", "Uploaded"


class MusicAssetVerificationStatus(models.TextChoices):
    VERIFIED = "verified", "Verified"
    PENDING = "pending", "Pending verification"
    MISSING = "missing", "Missing"


class MusicModerationStatus(models.TextChoices):
    NOT_REQUIRED = "not_required", "Not required"
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"


class MusicJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    CANCELLATION_REQUESTED = "cancellation_requested", "Cancellation requested"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class MusicJobStage(models.TextChoices):
    QUEUED = "queued", "Queued"
    PREPARING_REFERENCE = "preparing_reference", "Preparing reference"
    GENERATING = "generating", "Generating"
    POLLING = "polling", "Polling provider"
    VALIDATING = "validating", "Validating output"
    STORING = "storing", "Storing output"
    FINALIZED = "finalized", "Finalized"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class MusicVariantStatus(models.TextChoices):
    GENERATED = "generated", "Generated"
    FAILED = "failed", "Failed"


class MusicAsset(models.Model):
    """One immutable audio object plus verified metadata and provenance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="music_assets",
    )
    file = models.FileField(upload_to="projects/music/assets/", max_length=500)
    asset_role = models.CharField(max_length=16, choices=MusicAssetRole.choices)
    origin = models.CharField(max_length=16, choices=MusicAssetOrigin.choices)
    original_name = models.CharField(max_length=255, blank=True, default="")
    mime_type = models.CharField(max_length=64, blank=True, default="")
    size_bytes = models.BigIntegerField(null=True, blank=True)
    checksum_sha256 = models.CharField(max_length=64, blank=True, default="")
    duration_seconds = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        null=True,
        blank=True,
    )
    verification_status = models.CharField(
        max_length=24,
        choices=MusicAssetVerificationStatus.choices,
        default=MusicAssetVerificationStatus.VERIFIED,
    )
    moderation_status = models.CharField(
        max_length=16,
        choices=MusicModerationStatus.choices,
        default=MusicModerationStatus.NOT_REQUIRED,
    )
    rights_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_music_reference_assets",
    )
    rights_confirmed_at = models.DateTimeField(null=True, blank=True)
    rights_statement_version = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )
    provider = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    provider_request_id = models.CharField(max_length=255, blank=True, default="")
    provenance = models.JSONField(default=dict, blank=True)
    waveform_peaks = models.JSONField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_music_assets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "asset_role", "-created_at"]),
            models.Index(fields=["verification_status"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(origin__in=MusicAssetOrigin.values),
                name="chk_music_asset_origin_canonical",
            ),
            models.CheckConstraint(
                check=models.Q(
                    verification_status__in=MusicAssetVerificationStatus.values
                ),
                name="chk_music_asset_verification_canonical",
            ),
            models.CheckConstraint(
                check=(
                    ~models.Q(
                        verification_status=MusicAssetVerificationStatus.VERIFIED
                    )
                    | (
                        ~models.Q(mime_type="")
                        & models.Q(size_bytes__gt=0)
                        & ~models.Q(checksum_sha256="")
                        & models.Q(duration_seconds__gt=0)
                    )
                ),
                name="chk_music_asset_verified_metadata",
            ),
            models.CheckConstraint(
                check=(
                    ~models.Q(asset_role=MusicAssetRole.REFERENCE)
                    | (
                        models.Q(origin=MusicAssetOrigin.UPLOAD)
                        & models.Q(rights_confirmed_by__isnull=False)
                        & models.Q(rights_confirmed_at__isnull=False)
                        & ~models.Q(rights_statement_version="")
                    )
                ),
                name="chk_music_reference_attestation",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.asset_role == MusicAssetRole.REFERENCE:
            if self.origin != MusicAssetOrigin.UPLOAD:
                errors["origin"] = "Reference assets must originate from upload."
            if not self.rights_confirmed_by_id:
                errors["rights_confirmed_by"] = (
                    "Reference assets require an attesting actor."
                )
            if not self.rights_confirmed_at or not self.rights_statement_version:
                errors["rights_statement_version"] = (
                    "Reference assets require a versioned rights attestation."
                )
        if self.verification_status == MusicAssetVerificationStatus.VERIFIED:
            if not self.mime_type:
                errors["mime_type"] = "Verified assets require a MIME type."
            if not self.size_bytes or self.size_bytes <= 0:
                errors["size_bytes"] = "Verified assets require a positive size."
            if len(self.checksum_sha256) != 64:
                errors["checksum_sha256"] = (
                    "Verified assets require a SHA-256 checksum."
                )
            if self.duration_seconds is None or self.duration_seconds <= 0:
                errors["duration_seconds"] = (
                    "Verified assets require a positive duration."
                )
        if errors:
            raise ValidationError(errors)

    def _validate_immutable_object(self) -> None:
        if self._state.adding or not self.pk:
            return
        original = type(self).objects.filter(pk=self.pk).values(
            "project_id", "file", "asset_role", "origin"
        ).first()
        if original is None:
            return
        current = {
            "project_id": self.project_id,
            "file": getattr(self.file, "name", ""),
            "asset_role": self.asset_role,
            "origin": self.origin,
        }
        if current != original:
            raise ValidationError("Music asset audio identity is immutable.")

    def save(self, *args, **kwargs):
        self._validate_immutable_object()
        self.clean()
        return super().save(*args, **kwargs)


class MusicGenerationJob(models.Model):
    """Durable request snapshot and fenced provider execution state."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="music_generation_jobs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="music_generation_jobs",
    )
    target_track = models.ForeignKey(
        MusicTrack,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generation_jobs",
    )
    reference_asset = models.ForeignKey(
        MusicAsset,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="generation_jobs",
    )
    brief = models.JSONField(default=dict)
    compiled_request = models.JSONField(default=dict)
    provider = models.CharField(max_length=64, default="mock")
    model_name = models.CharField(max_length=128, blank=True, default="")
    provider_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=32,
        choices=MusicJobStatus.choices,
        default=MusicJobStatus.QUEUED,
    )
    stage = models.CharField(
        max_length=32,
        choices=MusicJobStage.choices,
        default=MusicJobStage.QUEUED,
    )
    variant_count = models.PositiveSmallIntegerField(default=2)
    idempotency_key = models.CharField(max_length=128)
    request_fingerprint = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    provider_job_id = models.CharField(max_length=255, blank=True, default="")
    provider_reference_id = models.CharField(max_length=255, blank=True, default="")
    next_poll_at = models.DateTimeField(null=True, blank=True)
    retry_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retries",
    )
    cancellation_requested_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=128, blank=True, default="")
    error_detail = models.CharField(max_length=500, blank=True, default="")
    error_http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error_retryable = models.BooleanField(null=True, blank=True)
    provider_metadata = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "-created_at"]),
            models.Index(fields=["actor", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["status", "next_poll_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(variant_count__in=(1, 2)),
                name="chk_music_job_variant_count",
            ),
            models.CheckConstraint(
                check=models.Q(attempts__lte=models.F("max_attempts")),
                name="chk_music_job_attempts",
            ),
            models.UniqueConstraint(
                fields=["project", "actor", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_music_job_idempotency",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.target_track_id and self.project_id:
            if self.target_track.project_id != self.project_id:
                errors["target_track"] = "Target track must belong to the job project."
        if self.reference_asset_id and self.project_id:
            if self.reference_asset.project_id != self.project_id:
                errors["reference_asset"] = (
                    "Reference asset must belong to the job project."
                )
            elif self.reference_asset.asset_role != MusicAssetRole.REFERENCE:
                errors["reference_asset"] = "Job reference must be a reference asset."
        if self.retry_of_id and self.project_id:
            if self.retry_of.project_id != self.project_id:
                errors["retry_of"] = "Retry source must belong to the job project."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class MusicVariant(models.Model):
    """One generated candidate belonging to a durable music job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        MusicGenerationJob,
        on_delete=models.CASCADE,
        related_name="variants",
    )
    asset = models.OneToOneField(
        MusicAsset,
        on_delete=models.PROTECT,
        related_name="generated_variant",
    )
    variant_index = models.PositiveSmallIntegerField()
    seed = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=MusicVariantStatus.choices,
        default=MusicVariantStatus.GENERATED,
    )
    provider_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["variant_index"]
        indexes = [models.Index(fields=["job"])]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "variant_index"],
                name="uniq_music_job_variant_index",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.job_id and self.asset_id:
            if self.job.project_id != self.asset.project_id:
                errors["asset"] = "Variant asset must belong to the job project."
            if self.asset.asset_role != MusicAssetRole.GENERATED:
                errors["asset"] = "Variant asset must be generated audio."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class MusicTrackVersion(models.Model):
    """Immutable accepted version of a logical ``MusicTrack``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    track = models.ForeignKey(
        MusicTrack,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    asset = models.ForeignKey(
        MusicAsset,
        on_delete=models.PROTECT,
        related_name="track_versions",
    )
    brief_snapshot = models.JSONField(default=dict, blank=True)
    lyrics_snapshot = models.JSONField(default=list, blank=True)
    reference_asset = models.ForeignKey(
        MusicAsset,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reference_track_versions",
    )
    source_variant = models.OneToOneField(
        MusicVariant,
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
        related_name="created_music_track_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["track", "version_number"],
                name="uniq_music_track_version_number",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.track_id and self.asset_id:
            if self.track.project_id != self.asset.project_id:
                errors["asset"] = "Version asset must belong to the track project."
            if self.asset.asset_role != MusicAssetRole.GENERATED:
                errors["asset"] = "Track versions require generated audio."
        if self.reference_asset_id and self.track_id:
            if self.reference_asset.project_id != self.track.project_id:
                errors["reference_asset"] = (
                    "Reference snapshot must belong to the track project."
                )
            elif self.reference_asset.asset_role != MusicAssetRole.REFERENCE:
                errors["reference_asset"] = (
                    "Reference snapshot must be a reference asset."
                )
        if self.source_variant_id and self.track_id:
            if self.source_variant.job.project_id != self.track.project_id:
                errors["source_variant"] = (
                    "Source variant must belong to the track project."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            raise ValidationError("Music track versions are immutable.")
        self.clean()
        return super().save(*args, **kwargs)
