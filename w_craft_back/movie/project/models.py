from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from w_craft_back.movie.properties.models import Genre, Audience


class ProjectStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    IN_PROGRESS = "in_progress", "В работе"
    COMPLETED = "completed", "Завершён"
    ARCHIVED = "archived", "В архиве"


class ProjectFormat(models.TextChoices):
    SHORT_FILM = "short_film", "Короткометражный фильм"
    FEATURE_FILM = "feature_film", "Полнометражный фильм"
    SERIES = "series", "Сериал"
    CLIP = "clip", "Клип"
    COMMERCIAL = "commercial", "Реклама"
    OTHER = "other", "Другое"


class Project(models.Model):
    title = models.CharField(max_length=255)
    genres = models.ManyToManyField(Genre)
    format = models.CharField(max_length=32, choices=ProjectFormat.choices)
    audiences = models.ManyToManyField(Audience)
    annotation = models.TextField()
    synopsis = models.TextField()

    # Dashboard fields.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_projects",
    )
    slug = models.SlugField(max_length=255, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    cover_image = models.ImageField(
        upload_to="projects/covers/", null=True, blank=True
    )
    status = models.CharField(
        max_length=20,
        choices=ProjectStatus.choices,
        default=ProjectStatus.DRAFT,
    )
    is_favorite = models.BooleanField(default=False)
    generation_settings = models.JSONField(default=dict, blank=True)
    credit_budget_limit = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at", "-created_at"]
        indexes = [
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["owner", "updated_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["slug"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(format__in=ProjectFormat.values),
                name="chk_project_format_canonical",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(credit_budget_limit__isnull=True)
                    | models.Q(credit_budget_limit__gte=0)
                ),
                name="project_credit_budget_nonnegative",
            ),
        ]

    def __str__(self):
        return self.title
