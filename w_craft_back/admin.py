from django.contrib import admin

from w_craft_back.credits.models import (
    CreditAccount,
    CreditAdminAuditEvent,
    CreditLedgerEntry,
    GenerationCharge,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.dashboard_models import (
    ProjectTag,
    ProjectMember,
    Location,
    Scene,
    SceneCharacter,
    SceneStoryboard,
    MusicTrack,
    SceneMusic,
    ProjectAsset,
    ProjectProgress,
    ProjectActivity,
    VideoShot,
)


@admin.register(CreditAccount)
class CreditAccountAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "available_balance",
        "reserved_balance",
        "is_frozen",
        "updated_at",
    )
    search_fields = ("user__username",)
    raw_id_fields = ("user",)
    readonly_fields = (
        "user",
        "available_balance",
        "reserved_balance",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CreditLedgerEntry)
class CreditLedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "operation_type",
        "available_delta",
        "reserved_delta",
        "counterparty",
        "created_at",
    )
    list_filter = ("operation_type",)
    search_fields = ("account__user__username", "counterparty__username")
    raw_id_fields = ("account", "counterparty")
    readonly_fields = (
        "id",
        "account",
        "operation_type",
        "available_delta",
        "reserved_delta",
        "available_balance_after",
        "reserved_balance_after",
        "correlation_id",
        "counterparty",
        "idempotency_key",
        "description",
        "metadata",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GenerationCharge)
class GenerationChargeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "domain",
        "provider",
        "model_name",
        "status",
        "estimated_cost",
        "charged_amount",
        "created_at",
    )
    list_filter = ("domain", "provider", "status", "cost_is_estimate", "routing_mode")
    search_fields = ("account__user__username", "job_id", "model_name")
    raw_id_fields = ("account",)
    readonly_fields = tuple(
        field.name for field in GenerationCharge._meta.fields
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CreditAdminAuditEvent)
class CreditAdminAuditEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "event_type",
        "amount",
        "actor",
        "created_at",
    )
    list_filter = ("event_type",)
    search_fields = ("account__user__username", "actor__username", "reason")
    raw_id_fields = ("account", "actor")
    readonly_fields = tuple(
        field.name for field in CreditAdminAuditEvent._meta.fields
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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


@admin.register(SceneStoryboard)
class SceneStoryboardAdmin(admin.ModelAdmin):
    list_display = (
        "scene",
        "asset",
        "source_scene_version",
        "confirmed_scene_version",
        "updated_at",
    )
    raw_id_fields = ("scene", "asset", "created_by", "updated_by")


@admin.register(VideoShot)
class VideoShotAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "scene", "order", "final_asset", "updated_at")
    raw_id_fields = (
        "project",
        "scene",
        "final_asset",
        "created_by",
        "updated_by",
    )


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
