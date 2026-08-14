from django.contrib import admin

from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.dashboard_models import (
    ProjectTag,
    ProjectMember,
    Location,
    Scene,
    SceneCharacter,
    MusicTrack,
    SceneMusic,
    ProjectAsset,
    ProjectProgress,
    ProjectActivity,
)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "status", "owner", "is_favorite", "updated_at")
    list_filter = ("status", "is_favorite")
    search_fields = ("title", "summary", "synopsis", "slug")
    raw_id_fields = ("owner",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-updated_at",)


@admin.register(ProjectTag)
class ProjectTagAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "name", "created_at")
    list_filter = ("name",)
    search_fields = ("name",)
    raw_id_fields = ("project",)


@admin.register(ProjectMember)
class ProjectMemberAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "user", "role", "created_at")
    list_filter = ("role",)
    raw_id_fields = ("project", "user")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "name", "is_created", "updated_at")
    list_filter = ("is_created",)
    search_fields = ("name", "description")
    raw_id_fields = ("project",)


@admin.register(Scene)
class SceneAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "order", "title", "status", "location", "updated_at")
    list_filter = ("status",)
    search_fields = ("title", "description")
    raw_id_fields = ("project", "location")
    ordering = ("project", "order")


@admin.register(SceneCharacter)
class SceneCharacterAdmin(admin.ModelAdmin):
    list_display = ("id", "scene", "character", "role_in_scene")
    raw_id_fields = ("scene", "character")


@admin.register(MusicTrack)
class MusicTrackAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "title", "author", "duration_seconds", "updated_at")
    search_fields = ("title", "author")
    raw_id_fields = ("project",)


@admin.register(SceneMusic)
class SceneMusicAdmin(admin.ModelAdmin):
    list_display = ("id", "scene", "track", "start_time_seconds")
    raw_id_fields = ("scene", "track")


@admin.register(ProjectAsset)
class ProjectAssetAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "asset_type", "title", "uploaded_by", "created_at")
    list_filter = ("asset_type",)
    search_fields = ("title",)
    raw_id_fields = ("project", "uploaded_by")


@admin.register(ProjectProgress)
class ProjectProgressAdmin(admin.ModelAdmin):
    list_display = (
        "project",
        "overall_progress",
        "script_progress",
        "visual_progress",
        "audio_progress",
        "postproduction_progress",
        "updated_at",
    )
    raw_id_fields = ("project",)


@admin.register(ProjectActivity)
class ProjectActivityAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "activity_type", "title", "user", "created_at")
    list_filter = ("activity_type",)
    search_fields = ("title", "description")
    raw_id_fields = ("project", "user")
    ordering = ("-created_at",)
