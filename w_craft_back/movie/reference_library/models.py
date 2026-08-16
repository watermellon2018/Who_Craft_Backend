"""Durable models for project references, versions, jobs, and scene usage."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.project.dashboard_models import Location, ProjectAsset, Scene
from w_craft_back.movie.project.models import Project


class ReferenceCategory(models.TextChoices):
    LOCATION = "location", "Локация"
    PROP = "prop", "Предмет"
    WARDROBE = "wardrobe", "Костюм"
    VEHICLE = "vehicle", "Транспорт"
    SYMBOL = "symbol", "Символ"
    OTHER = "other", "Другое"


class ReferenceSourceType(models.TextChoices):
    UPLOAD = "upload", "Upload"
    GENERATED = "generated", "Generated"
    EDIT = "edit", "Edit"


class ReferenceOperation(models.TextChoices):
    GENERATE = "generate", "Generate"
    EDIT = "edit", "Edit"


class ReferenceJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    CANCELLATION_REQUESTED = "cancellation_requested", "Cancellation requested"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class ReferenceJobStage(models.TextChoices):
    QUEUED = "queued", "Queued"
    COMPILING = "compiling", "Compiling"
    GENERATING = "generating", "Generating"
    VALIDATING = "validating", "Validating"
    STORING = "storing", "Storing"
    FINALIZED = "finalized", "Finalized"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class ReferenceVariantStatus(models.TextChoices):
    GENERATED = "generated", "Generated"
    APPLIED = "applied", "Applied"
    DISCARDED = "discarded", "Discarded"


class ReferenceCharacterRelation(models.TextChoices):
    OWNS = "owns", "Owns"
    WEARS = "wears", "Wears"
    CARRIES = "carries", "Carries"
    USES = "uses", "Uses"
    IMPORTANT = "important", "Important"
    ASSOCIATED = "associated", "Associated"


class SceneReferenceUsage(models.TextChoices):
    ENVIRONMENT = "environment", "Environment"
    HERO_PROP = "hero_prop", "Hero prop"
    SET_DRESSING = "set_dressing", "Set dressing"
    WARDROBE = "wardrobe", "Wardrobe"
    VEHICLE = "vehicle", "Vehicle"
    SYMBOL = "symbol", "Symbol"
    OTHER = "other", "Other"


class ProjectReference(models.Model):
    """Mutable logical continuity reference within one project."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="references",
    )
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=16, choices=ReferenceCategory.choices)
    description = models.TextField(blank=True, default="")
    brief = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)
    location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_references",
    )
    active_version = models.ForeignKey(
        "ReferenceVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_for_references",
    )
    version = models.PositiveIntegerField(default=1)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_project_references",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_project_references",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-created_at"]
        indexes = [
            models.Index(fields=["project", "archived_at", "-updated_at"]),
            models.Index(fields=["project", "category", "archived_at"]),
            models.Index(fields=["location"]),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.location_id:
            if self.location.project_id != self.project_id:
                errors["location"] = "Location must belong to the reference project."
        if self.active_version_id:
            if self.active_version.reference_id != self.id:
                errors["active_version"] = (
                    "Active version must belong to the reference."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class ReferenceVersion(models.Model):
    """Immutable accepted image version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.ForeignKey(
        ProjectReference,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    asset = models.ForeignKey(
        ProjectAsset,
        on_delete=models.RESTRICT,
        related_name="reference_versions",
    )
    thumbnail_asset = models.ForeignKey(
        ProjectAsset,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="reference_thumbnail_versions",
    )
    source_type = models.CharField(max_length=16, choices=ReferenceSourceType.choices)
    source_variant = models.OneToOneField(
        "ReferenceVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_version",
    )
    brief_snapshot = models.JSONField(default=dict)
    compiled_prompt = models.TextField(blank=True, default="")
    negative_prompt = models.TextField(blank=True, default="")
    provider = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    seed = models.CharField(max_length=128, blank=True, default="")
    rights_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_reference_versions",
    )
    rights_confirmed_at = models.DateTimeField(null=True, blank=True)
    rights_statement_version = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reference_versions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["reference", "version_number"],
                name="uniq_reference_version_number",
            ),
            models.CheckConstraint(
                check=models.Q(source_type__in=ReferenceSourceType.values),
                name="chk_reference_version_source_canonical",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.asset_id and self.reference_id:
            if (
                self.asset.project_id != self.reference.project_id
                or self.asset.asset_type != "reference"
            ):
                errors["asset"] = (
                    "Version asset must be a reference asset from the same project."
                )
        if self.thumbnail_asset_id and self.reference_id:
            if self.thumbnail_asset.project_id != self.reference.project_id:
                errors["thumbnail_asset"] = "Thumbnail must belong to the same project."
        if (
            self.source_variant_id
            and self.source_variant.job.reference_id != self.reference_id
        ):
            errors["source_variant"] = "Variant must belong to the same reference."
        if self.source_type == ReferenceSourceType.UPLOAD and (
            not self.rights_confirmed_by_id
            or not self.rights_confirmed_at
            or not self.rights_statement_version
        ):
            errors["rights_statement_version"] = (
                "Uploaded versions require rights attestation."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Reference versions are immutable.")
        self.clean()
        return super().save(*args, **kwargs)


class ReferenceGenerationJob(models.Model):
    """Durable immutable request plus mutable fenced execution state."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="reference_jobs",
    )
    reference = models.ForeignKey(
        ProjectReference,
        on_delete=models.CASCADE,
        related_name="generation_jobs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reference_generation_jobs",
    )
    operation = models.CharField(max_length=16, choices=ReferenceOperation.choices)
    brief_snapshot = models.JSONField(default=dict)
    compiled_request = models.JSONField(default=dict)
    source_version = models.ForeignKey(
        ReferenceVersion,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="edit_jobs",
    )
    variant_count = models.PositiveSmallIntegerField(default=1)
    requested_model = models.CharField(max_length=128, blank=True, default="")
    provider_snapshot = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=128)
    request_fingerprint = models.CharField(max_length=64)
    status = models.CharField(
        max_length=32,
        choices=ReferenceJobStatus.choices,
        default=ReferenceJobStatus.QUEUED,
    )
    stage = models.CharField(
        max_length=16,
        choices=ReferenceJobStage.choices,
        default=ReferenceJobStage.QUEUED,
    )
    progress = models.PositiveSmallIntegerField(default=0)
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
    cancellation_requested_at = models.DateTimeField(null=True, blank=True)
    provider = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")
    provider_request_id = models.CharField(max_length=255, blank=True, default="")
    provider_metadata = models.JSONField(default=dict, blank=True)
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
            models.Index(fields=["reference", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["status", "lease_expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(variant_count__in=(1, 2, 4)),
                name="chk_reference_job_variant_count",
            ),
            models.CheckConstraint(
                check=models.Q(attempts__lte=models.F("max_attempts")),
                name="chk_reference_job_attempts",
            ),
            models.UniqueConstraint(
                fields=["project", "actor", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_reference_job_idempotency",
            ),
            models.UniqueConstraint(
                fields=["reference"],
                condition=models.Q(
                    status__in=(
                        ReferenceJobStatus.QUEUED,
                        ReferenceJobStatus.PROCESSING,
                        ReferenceJobStatus.CANCELLATION_REQUESTED,
                    )
                ),
                name="uniq_reference_active_job",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if (
            self.reference_id
            and self.project_id
            and self.reference.project_id != self.project_id
        ):
            errors["reference"] = "Reference must belong to the job project."
        if self.operation == ReferenceOperation.EDIT:
            if self.variant_count != 1:
                errors["variant_count"] = "Edit jobs must request one variant."
            if not self.source_version_id:
                errors["source_version"] = "Edit jobs require a source version."
        if (
            self.source_version_id
            and self.source_version.reference_id != self.reference_id
        ):
            errors["source_version"] = "Source version must belong to the reference."
        if errors:
            raise ValidationError(errors)


class ReferenceVariant(models.Model):
    """Validated provider output waiting for an explicit apply."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        ReferenceGenerationJob,
        on_delete=models.CASCADE,
        related_name="variants",
    )
    asset = models.ForeignKey(
        ProjectAsset,
        on_delete=models.RESTRICT,
        related_name="reference_variants",
    )
    thumbnail_asset = models.ForeignKey(
        ProjectAsset,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="reference_thumbnail_variants",
    )
    variant_index = models.PositiveSmallIntegerField()
    seed = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=ReferenceVariantStatus.choices,
        default=ReferenceVariantStatus.GENERATED,
    )
    provider_metadata = models.JSONField(default=dict, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["variant_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "variant_index"],
                name="uniq_reference_job_variant",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if (
            self.asset_id
            and self.job_id
            and self.asset.project_id != self.job.project_id
        ):
            raise ValidationError(
                {"asset": "Variant asset must belong to the job project."}
            )


class ReferenceCharacterLink(models.Model):
    reference = models.ForeignKey(
        ProjectReference,
        on_delete=models.CASCADE,
        related_name="character_links",
    )
    character = models.ForeignKey(
        StudioCharacter,
        on_delete=models.CASCADE,
        related_name="reference_links",
    )
    relation = models.CharField(
        max_length=16,
        choices=ReferenceCharacterRelation.choices,
    )
    note = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["reference", "character", "relation"],
                name="uniq_reference_character_relation",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.reference_id and self.character_id:
            if self.reference.project_id != self.character.project_id:
                raise ValidationError(
                    {"character": "Character must belong to the reference project."}
                )

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class SceneReference(models.Model):
    scene = models.ForeignKey(
        Scene,
        on_delete=models.CASCADE,
        related_name="reference_usages",
    )
    reference = models.ForeignKey(
        ProjectReference,
        on_delete=models.CASCADE,
        related_name="scene_usages",
    )
    version = models.ForeignKey(
        ReferenceVersion,
        on_delete=models.RESTRICT,
        related_name="scene_usages",
    )
    usage = models.CharField(max_length=20, choices=SceneReferenceUsage.choices)
    note = models.CharField(max_length=500, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_scene_references",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["scene", "reference"],
                name="uniq_scene_reference",
            )
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if (
            self.scene_id
            and self.reference_id
            and self.scene.project_id != self.reference.project_id
        ):
            errors["reference"] = "Reference must belong to the scene project."
        if (
            self.version_id
            and self.reference_id
            and self.version.reference_id != self.reference_id
        ):
            errors["version"] = "Pinned version must belong to the reference."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)
