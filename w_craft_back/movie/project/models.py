from django.conf import settings
from django.db import models

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.properties.models import Genre, Audience


class ProjectStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    IN_PROGRESS = "in_progress", "В работе"
    COMPLETED = "completed", "Завершён"
    ARCHIVED = "archived", "В архиве"


class Project(models.Model):
    # Legacy creator attribution. It is intentionally NOT an ownership signal.
    user = models.ForeignKey(
        UserKey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=255)
    image = models.ImageField(upload_to='project/poster/', blank=True, default='')
    genre = models.ManyToManyField(Genre)
    format = models.CharField(max_length=255)
    audience = models.ManyToManyField(Audience)
    annot = models.TextField()
    desc = models.TextField()

    # Dashboard fields.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_projects",
    )
    slug = models.SlugField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
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

    def __str__(self):
        return self.title
