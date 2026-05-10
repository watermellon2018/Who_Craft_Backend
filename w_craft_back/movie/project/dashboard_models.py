"""
Dashboard-related models for the project page (/project-list/project).

Note on Character: a new Character model is intentionally NOT introduced here.
The canonical character entity is `w_craft_back.character_studio.models.StudioCharacter`,
which already has a `project` FK, name, role choices, status, and timestamps.
SceneCharacter therefore links Scene -> StudioCharacter directly.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.project.models import Project


class ProjectMemberRole(models.TextChoices):
    OWNER = "owner", "Владелец"
    EDITOR = "editor", "Редактор"
    VIEWER = "viewer", "Просмотр"


class SceneStatus(models.TextChoices):
    DRAFT = "draft", "Черновик"
    SCRIPT_READY = "script_ready", "Сценарий готов"
    STORYBOARD_READY = "storyboard_ready", "Сториборд готов"
    RENDERING = "rendering", "Рендерится"
    COMPLETED = "completed", "Завершена"
    FAILED = "failed", "Ошибка"


class AssetType(models.TextChoices):
    IMAGE = "image", "Изображение"
    VIDEO = "video", "Видео"
    AUDIO = "audio", "Аудио"
    DOCUMENT = "document", "Документ"
    REFERENCE = "reference", "Референс"
    STORYBOARD = "storyboard", "Сториборд"
    MODEL_3D = "model_3d", "3D модель"


class ActivityType(models.TextChoices):
    CHARACTER_CREATED = "character_created", "Персонаж создан"
    CHARACTER_UPDATED = "character_updated", "Персонаж обновлён"
    SCENE_CREATED = "scene_created", "Сцена создана"
    SCENE_RENDER_COMPLETED = "scene_render_completed", "Рендер сцены завершён"
    MUSIC_ADDED = "music_added", "Музыка добавлена"
    LOCATION_CREATED = "location_created", "Локация создана"
    ASSET_UPLOADED = "asset_uploaded", "Файл загружен"
    PROJECT_UPDATED = "project_updated", "Проект обновлён"
    PROJECT_STATUS_CHANGED = "project_status_changed", "Статус проекта изменён"
    PROJECT_ARCHIVED = "project_archived", "Проект архивирован"


class GenerationJobType(models.TextChoices):
    CHARACTER_IMAGE = "character_image", "Генерация персонажа"
    SCENE_IMAGE = "scene_image", "Генерация сцены"
    REFERENCE_SHEET = "reference_sheet", "Референсы персонажа"
    VIDEO = "video", "Генерация видео"
    MUSIC = "music", "Генерация музыки"
    LOCATION = "location", "Генерация локации"


class ProjectGenerationJobStatus(models.TextChoices):
    QUEUED = "queued", "В очереди"
    PROCESSING = "processing", "В процессе"
    COMPLETED = "completed", "Завершено"
    FAILED = "failed", "Ошибка"
    CANCELLED = "cancelled", "Отменено"


class ProjectTag(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="tags",
    )
    name = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "name"],
                name="uniq_project_tag_name",
            ),
        ]
        indexes = [
            models.Index(fields=["project"]),
        ]

    def __str__(self):
        return f"{self.project_id}:{self.name}"


class ProjectMember(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="members",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=ProjectMemberRole.choices,
        default=ProjectMemberRole.VIEWER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "user"],
                name="uniq_project_member",
            ),
        ]
        indexes = [
            models.Index(fields=["project", "role"]),
        ]

    def __str__(self):
        return f"{self.user_id}@{self.project_id} ({self.role})"


class Location(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="locations",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    image = models.ImageField(upload_to="locations/", null=True, blank=True)
    is_created = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["project"]),
            models.Index(fields=["project", "is_created"]),
        ]

    def __str__(self):
        return self.name


class Scene(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="scenes",
    )
    title = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True, default="")
    script_text = models.TextField(blank=True, default="")
    location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scenes",
    )
    status = models.CharField(
        max_length=20,
        choices=SceneStatus.choices,
        default=SceneStatus.DRAFT,
    )
    preview_image = models.ImageField(
        upload_to="scenes/previews/", null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "created_at"]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["project", "order"]),
            models.Index(fields=["project", "updated_at"]),
        ]

    def __str__(self):
        return f"{self.order:02d} — {self.title}"


class SceneCharacter(models.Model):
    """Link table Scene <-> StudioCharacter (canonical character model)."""

    scene = models.ForeignKey(
        Scene,
        on_delete=models.CASCADE,
        related_name="scene_characters",
    )
    character = models.ForeignKey(
        StudioCharacter,
        on_delete=models.CASCADE,
        related_name="scene_appearances",
    )
    role_in_scene = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scene", "character"],
                name="uniq_scene_character",
            ),
        ]


class MusicTrack(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="music_tracks",
    )
    title = models.CharField(max_length=255)
    author = models.CharField(max_length=255, blank=True, default="")
    audio_file = models.FileField(
        upload_to="projects/music/", null=True, blank=True
    )
    cover_image = models.ImageField(
        upload_to="projects/music/covers/", null=True, blank=True
    )
    duration_seconds = models.PositiveIntegerField(default=0)
    tags = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["project"]),
            models.Index(fields=["project", "updated_at"]),
        ]

    def __str__(self):
        return self.title


class SceneMusic(models.Model):
    scene = models.ForeignKey(
        Scene,
        on_delete=models.CASCADE,
        related_name="scene_music",
    )
    track = models.ForeignKey(
        MusicTrack,
        on_delete=models.CASCADE,
        related_name="scene_usages",
    )
    start_time_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scene", "track"],
                name="uniq_scene_music",
            ),
        ]


class ProjectAsset(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="assets",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_project_assets",
    )
    file = models.FileField(upload_to="projects/assets/")
    asset_type = models.CharField(max_length=20, choices=AssetType.choices)
    title = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["project", "asset_type"]),
            models.Index(fields=["project", "created_at"]),
            models.Index(fields=["uploaded_by"]),
        ]

    def __str__(self):
        return f"{self.asset_type}:{self.title or self.file.name}"


class ProjectProgress(models.Model):
    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name="progress",
    )
    overall_progress = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    script_progress = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    visual_progress = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    audio_progress = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    postproduction_progress = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Progress[{self.project_id}] {self.overall_progress}%"


class ProjectActivity(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_activities",
    )
    activity_type = models.CharField(max_length=40, choices=ActivityType.choices)
    title = models.CharField(max_length=255)
    description = models.CharField(max_length=500, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "created_at"]),
            models.Index(fields=["activity_type"]),
        ]

    def __str__(self):
        return f"{self.activity_type}: {self.title}"


class ProjectGenerationJob(models.Model):
    """Project-level generation job. Distinct from CharacterGenerationJob in
    character_studio.models, which is scoped to a specific StudioCharacter."""

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="generation_jobs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_generation_jobs",
    )
    job_type = models.CharField(max_length=30, choices=GenerationJobType.choices)
    status = models.CharField(
        max_length=20,
        choices=ProjectGenerationJobStatus.choices,
        default=ProjectGenerationJobStatus.QUEUED,
    )
    prompt = models.TextField(blank=True, default="")
    negative_prompt = models.TextField(blank=True, default="")
    input_data = models.JSONField(default=dict, blank=True)
    output_data = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["job_type", "status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.job_type}#{self.id} [{self.status}]"
