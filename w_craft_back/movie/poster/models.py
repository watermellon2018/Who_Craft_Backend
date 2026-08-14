"""Poster generation models for the project poster workspace.

Three entities, owned by ``w_craft_back`` so existing migrations stay coherent:

* ``ProjectPoster`` — one-to-one with ``Project``; tracks the currently selected
  variant and the high-level poster status.
* ``PosterGenerationJob`` — one row per "Сгенерировать постер" click; carries
  the prompt, style, format, optional reference, and the AI worker state.
* ``PosterVariant`` — one row per image returned by the model; the strip on the
  page shows recent rows and the user picks one to be the project's poster.

We deliberately follow the existing project conventions: integer auto-IDs (no
UUID PKs — the rest of the backend uses ``int`` keys, see ``Project.id``),
``TextChoices`` for status enums, ``ImageField`` instead of opaque storage
keys (so the file lands under ``MEDIA_ROOT`` like every other upload).
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from w_craft_back.movie.project.dashboard_models import ProjectAsset
from w_craft_back.movie.project.models import Project


# Surface enums as ``models.TextChoices`` so they render as native CharField
# choices in admin / DRF and so we don't need separate CHECK constraints.

class ProjectPosterStatus(models.TextChoices):
    EMPTY = "empty", "Нет постера"
    GENERATING = "generating", "Генерируется"
    READY = "ready", "Готов"
    FAILED = "failed", "Ошибка"


class PosterJobStatus(models.TextChoices):
    CANCELLATION_REQUESTED = "cancellation_requested", "Cancellation requested"
    QUEUED = "queued", "В очереди"
    PROCESSING = "processing", "В процессе"
    COMPLETED = "completed", "Завершено"
    FAILED = "failed", "Ошибка"
    CANCELLED = "cancelled", "Отменено"


class PosterJobOperation(models.TextChoices):
    GENERATE = "generate", "Генерация"
    EDIT = "edit", "Редактирование"


class PosterStyle(models.TextChoices):
    CINEMATIC = "cinematic", "Кинематографичный"
    ANIME = "anime", "Аниме"
    DARK_FANTASY = "dark_fantasy", "Тёмное фэнтези"
    REALISM = "realism", "Реализм"


class PosterFormat(models.TextChoices):
    VERTICAL = "vertical", "Вертикальный"
    SQUARE = "square", "Квадратный"
    HORIZONTAL = "horizontal", "Горизонтальный"


# Format → (aspect, width, height). Centralized so jobs and the variants they
# produce stay consistent without a separate ``poster_formats`` table (deferred
# until a real config UI exists — MVP keeps this in code).
POSTER_FORMAT_DIMENSIONS: dict[str, tuple[str, int, int]] = {
    PosterFormat.VERTICAL: ("2:3", 1024, 1536),
    PosterFormat.SQUARE: ("1:1", 1024, 1024),
    PosterFormat.HORIZONTAL: ("16:9", 1536, 864),
}


class ProjectPoster(models.Model):
    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name="poster",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_posters",
    )
    # FK is set after a user picks a variant. ``SET_NULL`` because soft-deleting
    # the chosen variant must not cascade-delete the poster row.
    selected_variant = models.ForeignKey(
        "PosterVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="selected_for",
    )
    status = models.CharField(
        max_length=20,
        choices=ProjectPosterStatus.choices,
        default=ProjectPosterStatus.EMPTY,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["status"]),
        ]

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.selected_variant_id:
            if self.selected_variant.project_id != self.project_id:
                errors["selected_variant"] = (
                    "Selected variant must belong to the poster project."
                )
            if not self.pk or self.selected_variant.poster_id != self.pk:
                errors["selected_variant"] = (
                    "Selected variant must belong to this poster."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Poster[{self.project_id}] {self.status}"


class PosterGenerationJob(models.Model):
    poster = models.ForeignKey(
        ProjectPoster,
        on_delete=models.CASCADE,
        related_name="jobs",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="poster_jobs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="poster_generation_jobs",
    )

    operation = models.CharField(
        max_length=16,
        choices=PosterJobOperation.choices,
        default=PosterJobOperation.GENERATE,
    )
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    request_hash = models.CharField(max_length=64, blank=True, default="")
    requested_model = models.CharField(max_length=128, blank=True, default="")
    reference_storage_key = models.CharField(max_length=500, blank=True, default="")
    reference_mime_type = models.CharField(max_length=100, blank=True, default="")
    progress = models.PositiveSmallIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    cancellation_requested_at = models.DateTimeField(null=True, blank=True)
    retry_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retries",
    )

    prompt = models.TextField()
    negative_prompt = models.TextField(blank=True, default="")

    style = models.CharField(max_length=64, choices=PosterStyle.choices)
    format = models.CharField(max_length=32, choices=PosterFormat.choices)
    aspect_ratio = models.CharField(max_length=16)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    # Reference image: kept in two parallel slots, like other upload-aware
    # entities in this codebase. Either may be null.
    reference_image_url = models.TextField(blank=True, default="")
    reference_asset = models.ForeignKey(
        ProjectAsset,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="poster_jobs",
    )
    source_variant = models.ForeignKey(
        "PosterVariant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="edit_jobs",
    )

    model_provider = models.CharField(max_length=64, blank=True, default="")
    model_name = models.CharField(max_length=128, blank=True, default="")

    status = models.CharField(
        max_length=32,
        choices=PosterJobStatus.choices,
        default=PosterJobStatus.QUEUED,
    )
    credits_cost = models.PositiveIntegerField(default=1)

    error_message = models.TextField(blank=True, default="")
    error_code = models.CharField(max_length=128, blank=True, default="")
    error_http_status = models.PositiveSmallIntegerField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "-created_at"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["poster", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(progress__gte=0) & models.Q(progress__lte=100),
                name="chk_poster_job_progress_range",
            ),
            models.CheckConstraint(
                check=models.Q(attempts__lte=models.F("max_attempts")),
                name="chk_poster_job_attempts",
            ),
            models.UniqueConstraint(
                fields=[
                    "project",
                    "user",
                    "operation",
                    "idempotency_key",
                ],
                condition=~models.Q(idempotency_key=""),
                name="uniq_poster_job_idempotency_key",
            ),
        ]

    def __str__(self) -> str:
        return f"PosterJob#{self.id} [{self.status}]"

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.poster_id and self.project_id:
            if self.poster.project_id != self.project_id:
                errors["poster"] = (
                    "Poster must belong to the generation job project."
                )
        if self.reference_asset_id and self.project_id:
            if self.reference_asset.project_id != self.project_id:
                errors["reference_asset"] = (
                    "Reference asset must belong to the generation job project."
                )
        if self.source_variant_id and self.project_id:
            if self.source_variant.project_id != self.project_id:
                errors["source_variant"] = (
                    "Source variant must belong to the generation job project."
                )
        if self.source_variant_id and self.poster_id:
            if self.source_variant.poster_id != self.poster_id:
                errors["source_variant"] = (
                    "Source variant must belong to the generation job poster."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class PosterProviderCircuit(models.Model):
    provider_key = models.CharField(max_length=255, unique=True)
    failure_count = models.PositiveSmallIntegerField(default=0)
    opened_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.provider_key}: failures={self.failure_count}"


class PosterVariant(models.Model):
    job = models.ForeignKey(
        PosterGenerationJob,
        on_delete=models.CASCADE,
        related_name="variants",
    )
    poster = models.ForeignKey(
        ProjectPoster,
        on_delete=models.CASCADE,
        related_name="variants",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="poster_variants",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="poster_variants",
    )

    image = models.ImageField(upload_to="projects/posters/variants/")
    thumbnail = models.ImageField(
        upload_to="projects/posters/thumbnails/", null=True, blank=True
    )
    # Mirror image_url/thumbnail_url for cases where the worker received a URL
    # from an external provider before/instead of downloading the file.
    image_url = models.TextField(blank=True, default="")
    thumbnail_url = models.TextField(blank=True, default="")

    variant_index = models.PositiveSmallIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    mime_type = models.CharField(max_length=64, blank=True, default="")
    seed = models.BigIntegerField(null=True, blank=True)

    is_selected = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "-created_at"]),
            models.Index(fields=["poster", "-created_at"]),
            models.Index(fields=["job"]),
            # Partial index for "currently selected variant per project".
            models.Index(
                fields=["project"],
                name="poster_variant_selected_idx",
                condition=models.Q(is_selected=True),
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "variant_index"],
                name="uniq_poster_job_variant_index",
            ),
        ]

    def __str__(self) -> str:
        return f"PosterVariant#{self.id} job={self.job_id} sel={self.is_selected}"

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.poster_id and self.project_id:
            if self.poster.project_id != self.project_id:
                errors["poster"] = "Poster must belong to the variant project."
        if self.job_id and self.project_id:
            if self.job.project_id != self.project_id:
                errors["job"] = "Job must belong to the variant project."
        if self.job_id and self.poster_id:
            if self.job.poster_id != self.poster_id:
                errors["job"] = "Job must belong to the variant poster."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)
