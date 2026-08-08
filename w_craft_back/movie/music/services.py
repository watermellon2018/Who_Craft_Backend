"""Project policy, ORM orchestration, and public serialization for Music Studio."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.utils import timezone

from w_craft_back.movie.music.errors import (
    CannotCancel,
    JobNotFound,
    MusicError,
    PermissionDenied,
    ProjectNotFound,
    ReferenceInvalid,
    ReferenceNotFound,
    SceneNotFound,
    TrackNotFound,
    ValidationError,
    VariantNotFound,
    VersionConflict,
    public_provider_error_detail,
)
from w_craft_back.movie.music.serializers import (
    CONTENT_MODES,
    ENERGY_CURVES,
    GENRES,
    INSTRUMENTS,
    LYRICS_LANGUAGES,
    LYRICS_SECTION_TYPES,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MOODS,
    PURPOSES,
    TEMPO_MODES,
    VARIANT_COUNTS,
    VOCAL_DELIVERIES,
    VOCAL_DENSITIES,
    VOCAL_TIMBRES,
    music_max_duration_seconds,
    music_max_lyrics_chars,
    music_min_duration_seconds,
)
from w_craft_back.movie.project import policy, project_mutations
from w_craft_back.movie.project.dashboard_models import (
    MusicTrack,
    Scene,
    SceneCharacter,
    SceneMusic,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.services import record_activity
from w_craft_back.storage_gateway import (
    StorageGatewayError,
    signed_url_for_file,
)


TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
CANCELLABLE_JOB_STATUSES = {"queued", "processing", "cancellation_requested"}
RETRYABLE_JOB_STATUSES = {"failed", "cancelled"}
SCENE_SUMMARY_MAX_LENGTH = 240
MAX_LIBRARY_PAGE_SIZE = 100
MAX_SCENE_OPTIONS = 100


def _music_models():
    from w_craft_back.movie.music.models import (
        MusicAsset,
        MusicGenerationJob,
        MusicTrackVersion,
        MusicVariant,
    )

    return MusicAsset, MusicGenerationJob, MusicTrackVersion, MusicVariant


def _adapt_domain_error(error: Exception) -> MusicError | None:
    """Translate lifecycle/provider/compiler failures at one boundary."""

    from w_craft_back.movie.music.lifecycle import MusicLifecycleError
    from w_craft_back.movie.music.prompt_compiler import MusicBriefError
    from w_craft_back.movie.music.providers.base import MusicProviderError

    if not isinstance(
        error,
        (MusicLifecycleError, MusicBriefError, MusicProviderError),
    ):
        return None
    code = getattr(error, "code", None)
    http_status = getattr(error, "http_status", None)
    if not isinstance(code, str) or not code or not isinstance(http_status, int):
        return None
    detail = str(
        getattr(error, "message", None)
        or getattr(error, "detail", None)
        or "Music Studio could not complete the operation."
    )
    return MusicError(
        detail,
        code=code,
        http_status=http_status,
        retryable=getattr(error, "retryable", False),
    )


def _iso_datetime(value) -> str | None:
    if value is None:
        return None
    rendered = value.isoformat()
    return rendered.replace("+00:00", "Z")


def _coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
    field: str,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Validation failed.", errors={field: ["Must be an integer."]}
        ) from exc
    if not minimum <= parsed <= maximum:
        raise ValidationError(
            "Validation failed.",
            errors={field: [f"Must be between {minimum} and {maximum}."]},
        )
    return parsed


def _project_for_action(
    actor: User,
    project_id: int,
    action: policy.Action,
    *,
    lock: bool = False,
) -> Project:
    if lock:
        queryset = Project.objects.select_for_update()
    else:
        queryset = Project.objects.select_related("owner", "user")
    project = queryset.filter(pk=project_id).first()
    if project is None or not policy.can(actor, project, policy.Action.VIEW):
        raise ProjectNotFound("Project was not found.")
    if not policy.can(actor, project, action):
        raise PermissionDenied("You do not have permission for this operation.")
    return project


def _permission_payload(actor: User, project: Project) -> dict[str, bool | str | None]:
    summary = policy.permission_summary(actor, project)
    return {
        "currentUserRole": summary["currentUserRole"],
        "canView": summary["canView"],
        "canEdit": summary["canEdit"],
        "canRunGeneration": summary["canRunGeneration"],
    }


def _signed_expiry(audio_url: str | None) -> str | None:
    if not audio_url:
        return None
    ttl = max(1, int(getattr(settings, "SIGNED_MEDIA_TTL_SECONDS", 300)))
    return _iso_datetime(timezone.now() + timedelta(seconds=ttl))


def _asset_url(asset, request) -> str | None:
    if asset is None:
        return None
    from w_craft_back.storage_gateway import signed_url_for_music_asset

    return signed_url_for_music_asset(asset, request=request)


def _asset_payload(asset, request, *, include_name: bool = False) -> dict | None:
    verification_statuses = {
        "verified": "accepted",
        "legacy_unverified": "pending",
        "missing": "rejected",
    }
    if asset is None:
        return None
    audio_url = _asset_url(asset, request)
    payload = {
        "assetId": str(asset.id),
        "durationSeconds": (
            float(asset.duration_seconds)
            if asset.duration_seconds is not None
            else None
        ),
        "mimeType": asset.mime_type or None,
        "audioUrl": audio_url,
        "audioUrlExpiresAt": _signed_expiry(audio_url),
        "localVerificationStatus": verification_statuses.get(
            asset.verification_status,
            "pending",
        ),
        "providerModerationStatus": asset.moderation_status,
    }
    if include_name:
        payload["name"] = asset.original_name or "reference-audio"
    return payload


def _version_payload(version, request) -> dict | None:
    if version is None:
        return None
    asset = version.asset
    audio_url = _asset_url(asset, request)
    provider_provenance = dict(asset.provenance or {})
    safe_provenance = {
        key: provider_provenance[key]
        for key in ("watermark", "watermarkStatus", "c2pa", "synthId")
        if key in provider_provenance
    }
    return {
        "versionId": str(version.id),
        "versionNumber": version.version_number,
        "durationSeconds": (
            float(asset.duration_seconds)
            if asset.duration_seconds is not None
            else None
        ),
        "mimeType": asset.mime_type or None,
        "audioUrl": audio_url,
        "audioUrlExpiresAt": _signed_expiry(audio_url),
        "brief": dict(version.brief_snapshot or {}),
        "lyrics": list(version.lyrics_snapshot or []),
        "referenceAssetId": (
            str(version.reference_asset_id) if version.reference_asset_id else None
        ),
        "createdAt": _iso_datetime(version.created_at),
        "createdById": version.created_by_id,
        "provenance": {
            **safe_provenance,
            "createdByAi": asset.origin == "generated",
            "provider": asset.provider or None,
            "model": asset.model_name or None,
            "providerRequestId": asset.provider_request_id or None,
        },
    }


def _legacy_active_version_payload(track: MusicTrack, request) -> dict | None:
    if not track.audio_file:
        return None
    audio_url = signed_url_for_file(track.audio_file, request, project=track.project)
    return {
        "versionId": None,
        "versionNumber": None,
        "durationSeconds": track.duration_seconds or None,
        "mimeType": None,
        "audioUrl": audio_url,
        "audioUrlExpiresAt": _signed_expiry(audio_url),
        "legacy": True,
    }


def _track_summary_payload(track: MusicTrack, request) -> dict:
    active = (
        _version_payload(track.active_version, request)
        if track.active_version_id
        else _legacy_active_version_payload(track, request)
    )
    return {
        "id": track.id,
        "title": track.title,
        "author": track.author,
        "tags": list(track.tags or []),
        "status": "archived" if track.archived_at else "active",
        "source": track.source,
        "version": track.version,
        "activeVersion": active,
        "usageCount": int(getattr(track, "usage_count", 0)),
        "updatedAt": _iso_datetime(track.updated_at),
    }


def _track_queryset():
    return MusicTrack.objects.select_related(
        "project",
        "active_version__asset",
        "active_version__asset__project",
        "active_version__reference_asset",
    )


def list_tracks(
    *,
    actor: User,
    project_id: int,
    request,
    query: str = "",
    status_filter: str = "active",
    limit: Any = 30,
    offset: Any = 0,
) -> dict:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    page_limit = _coerce_int(
        limit,
        default=30,
        minimum=1,
        maximum=MAX_LIBRARY_PAGE_SIZE,
        field="limit",
    )
    page_offset = _coerce_int(
        offset,
        default=0,
        minimum=0,
        maximum=2_147_483_647,
        field="offset",
    )
    if status_filter not in {"active", "archived", "all"}:
        raise ValidationError(
            "Validation failed.",
            errors={"status": ["Must be active, archived, or all."]},
        )

    queryset = _track_queryset().filter(project=project)
    if status_filter == "active":
        queryset = queryset.filter(archived_at__isnull=True)
    elif status_filter == "archived":
        queryset = queryset.filter(archived_at__isnull=False)
    normalized_query = str(query or "").strip()
    if normalized_query:
        queryset = queryset.filter(
            Q(title__icontains=normalized_query)
            | Q(author__icontains=normalized_query)
        )
    queryset = queryset.annotate(
        usage_count=Count("scene_usages", distinct=True)
    ).order_by("-updated_at", "-id")
    total = queryset.count()
    items = list(queryset[page_offset:page_offset + page_limit])
    return {
        "items": [_track_summary_payload(track, request) for track in items],
        "page": {"limit": page_limit, "offset": page_offset, "total": total},
        "permissions": _permission_payload(actor, project),
    }


def create_legacy_metadata_track(
    *, actor: User, project_id: int, data: Mapping[str, Any]
) -> dict:
    _project_for_action(actor, project_id, policy.Action.EDIT_CONTENT)
    try:
        track = project_mutations.create_music_track(
            actor=actor,
            action=policy.Action.EDIT_CONTENT,
            project_id=project_id,
            data=data,
        )
    except Project.DoesNotExist as exc:
        raise ProjectNotFound("Project was not found.") from exc
    except project_mutations.ProjectMutationForbidden as exc:
        raise PermissionDenied("You do not have permission for this operation.") from exc
    return {"id": track.id, "title": track.title}


def get_capabilities(*, actor: User, project_id: int) -> dict:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    capabilities = {
        "contentModes": list(CONTENT_MODES),
        "variantCounts": list(VARIANT_COUNTS),
        "duration": {
            "minSeconds": music_min_duration_seconds(),
            "maxSeconds": music_max_duration_seconds(),
            "defaultSeconds": min(30, music_max_duration_seconds()),
        },
        "outputFormats": ["wav"],
        "briefFields": {
            "purposes": list(PURPOSES),
            "genres": list(GENRES),
            "moods": list(MOODS),
            "instruments": list(INSTRUMENTS),
            "energyCurves": list(ENERGY_CURVES),
            "tempoModes": list(TEMPO_MODES),
            "vocalStyles": {
                "timbres": list(VOCAL_TIMBRES),
                "deliveries": list(VOCAL_DELIVERIES),
                "densities": list(VOCAL_DENSITIES),
            },
        },
        "lyrics": {
            "supported": True,
            "languages": list(LYRICS_LANGUAGES),
            "maxChars": music_max_lyrics_chars(),
            "sectionTypes": list(LYRICS_SECTION_TYPES),
        },
        "audioReference": {
            "supported": True,
            "maxCount": 1,
            "formats": ["mp3", "wav", "ogg"],
            "maxBytes": int(
                getattr(settings, "MUSIC_MAX_REFERENCE_BYTES", 50 * 1024 * 1024)
            ),
            "minSeconds": int(
                getattr(settings, "MUSIC_MIN_REFERENCE_DURATION_SECONDS", 1)
            ),
            "maxSeconds": int(
                getattr(settings, "MUSIC_MAX_REFERENCE_DURATION_SECONDS", 300)
            ),
        },
        "supportsSeed": True,
        "supportsCancellation": True,
        "providerDisplayName": "Music generator",
        "permissions": _permission_payload(actor, project),
    }
    from w_craft_back.movie.music.providers.registry import (
        get_music_provider_capabilities,
    )

    try:
        capabilities.update(get_music_provider_capabilities())
    except Exception as error:
        adapted = _adapt_domain_error(error)
        if adapted is None:
            raise
        raise adapted from error
    capabilities["permissions"] = _permission_payload(actor, project)
    return capabilities


def _scene_summary(scene: Scene) -> str:
    raw = scene.description or scene.script_text or ""
    summary = " ".join(str(raw).split())
    if len(summary) <= SCENE_SUMMARY_MAX_LENGTH:
        return summary
    return f"{summary[: SCENE_SUMMARY_MAX_LENGTH - 1].rstrip()}…"


def _scene_character_prefetch(lookup: str) -> Prefetch:
    return Prefetch(
        lookup,
        queryset=SceneCharacter.objects.select_related("character").order_by("id"),
        to_attr="_music_character_links",
    )


def _scene_option_payload(scene: Scene) -> dict:
    return {
        "sceneId": scene.id,
        "number": scene.order,
        "act": scene.act,
        "title": scene.title,
        "location": scene.location.name if scene.location_id else "",
        "summary": _scene_summary(scene),
        "mood": scene.mood,
        "durationSeconds": scene.duration_seconds,
        "characters": [
            link.character.name
            for link in getattr(scene, "_music_character_links", ())
        ],
    }


def list_scene_options(
    *,
    actor: User,
    project_id: int,
    query: str = "",
    act: Any = None,
    limit: Any = 20,
    scene_id: Any = None,
) -> dict:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    page_limit = _coerce_int(
        limit,
        default=20,
        minimum=1,
        maximum=MAX_SCENE_OPTIONS,
        field="limit",
    )
    queryset = Scene.objects.filter(project=project).select_related("location")
    if scene_id not in (None, ""):
        parsed_scene_id = _coerce_int(
            scene_id,
            default=0,
            minimum=1,
            maximum=2_147_483_647,
            field="sceneId",
        )
        queryset = queryset.filter(pk=parsed_scene_id)
    elif act not in (None, ""):
        parsed_act = _coerce_int(
            act,
            default=1,
            minimum=1,
            maximum=3,
            field="act",
        )
        queryset = queryset.filter(act=parsed_act)
    normalized_query = str(query or "").strip()
    if scene_id in (None, "") and normalized_query:
        search = (
            Q(title__icontains=normalized_query)
            | Q(location__name__icontains=normalized_query)
            | Q(description__icontains=normalized_query)
            | Q(script_text__icontains=normalized_query)
            | Q(scene_characters__character__name__icontains=normalized_query)
        )
        if normalized_query.isdigit():
            search |= Q(order=int(normalized_query))
        queryset = queryset.filter(search).distinct()
    scenes = list(
        queryset.prefetch_related(
            _scene_character_prefetch("scene_characters")
        ).order_by("order", "id")[:page_limit]
    )
    return {
        "items": [_scene_option_payload(scene) for scene in scenes],
        "nextCursor": None,
        "permissions": _permission_payload(actor, project),
    }


def create_reference_asset(
    *,
    actor: User,
    project_id: int,
    upload,
    rights_statement_version: str,
    request,
) -> dict:
    project = _project_for_action(actor, project_id, policy.Action.RUN_GENERATION)
    from w_craft_back.storage_gateway import store_music_reference_upload

    try:
        stored = store_music_reference_upload(upload, project_id=project.id)
    except StorageGatewayError as exc:
        raise ReferenceInvalid(exc.message) from exc
    MusicAsset, _MusicGenerationJob, _MusicTrackVersion, _MusicVariant = (
        _music_models()
    )
    try:
        with transaction.atomic():
            asset = MusicAsset(
                project=project,
                asset_role="reference",
                origin="upload",
                original_name=getattr(stored, "original_name", upload.name),
                mime_type=stored.mime_type,
                size_bytes=stored.size_bytes,
                checksum_sha256=stored.sha256,
                duration_seconds=stored.duration_seconds,
                verification_status="verified",
                moderation_status="pending",
                rights_confirmed_by=actor,
                rights_confirmed_at=timezone.now(),
                rights_statement_version=rights_statement_version,
                created_by=actor,
            )
            asset.file.name = stored.storage_key
            asset.save()
    except Exception:
        from w_craft_back.storage_gateway import delete_storage_key

        delete_storage_key(stored.storage_key)
        raise
    payload = _asset_payload(asset, request, include_name=True) or {}
    payload["permissions"] = _permission_payload(actor, project)
    return payload


@transaction.atomic
def delete_reference_asset(
    *, actor: User, project_id: int, asset_id
) -> None:
    project = _project_for_action(
        actor,
        project_id,
        policy.Action.RUN_GENERATION,
        lock=True,
    )
    MusicAsset, MusicGenerationJob, MusicTrackVersion, _MusicVariant = _music_models()
    asset = (
        MusicAsset.objects.select_for_update()
        .filter(pk=asset_id, project=project, asset_role="reference")
        .first()
    )
    if asset is None:
        raise ReferenceNotFound("Reference asset was not found.")
    if (
        MusicGenerationJob.objects.filter(reference_asset=asset).exists()
        or MusicTrackVersion.objects.filter(reference_asset=asset).exists()
    ):
        raise ReferenceInvalid(
            "A reference used by a generation or track version cannot be deleted.",
            http_status=409,
        )
    asset.delete()


def _assignment_payload(assignment: SceneMusic) -> dict:
    scene = assignment.scene
    return {
        "sceneId": scene.id,
        "sceneNumber": scene.order,
        "sceneTitle": scene.title,
        "location": scene.location.name if scene.location_id else "",
        "scene": _scene_option_payload(scene),
        "trackVersionId": (
            str(assignment.track_version_id)
            if assignment.track_version_id
            else None
        ),
        "trackVersionNumber": (
            assignment.track_version.version_number
            if assignment.track_version_id
            else None
        ),
        "startTimeSeconds": assignment.start_time_seconds,
    }


def _track_detail_queryset():
    _MusicAsset, _MusicGenerationJob, MusicTrackVersion, _MusicVariant = (
        _music_models()
    )
    assignments = (
        SceneMusic.objects.select_related(
            "scene__location",
            "track_version",
        )
        .prefetch_related(
            _scene_character_prefetch("scene__scene_characters")
        )
        .order_by("scene__order", "scene_id")
    )
    versions = MusicTrackVersion.objects.select_related(
        "asset__project",
        "reference_asset",
        "created_by",
    ).order_by("-version_number")
    return _track_queryset().prefetch_related(
        Prefetch("versions", queryset=versions, to_attr="_music_versions"),
        Prefetch(
            "scene_usages",
            queryset=assignments,
            to_attr="_music_assignments",
        ),
    )


def _track_detail_payload(
    track: MusicTrack,
    actor: User,
    request,
) -> dict:
    payload = _track_summary_payload(track, request)
    payload.update(
        {
            "versions": [
                _version_payload(version, request)
                for version in track._music_versions
            ],
            "assignments": [
                _assignment_payload(assignment)
                for assignment in track._music_assignments
            ],
            "usageCount": len(track._music_assignments),
            "permissions": _permission_payload(actor, track.project),
        }
    )
    return payload


def get_track(
    *, actor: User, project_id: int, track_id: int, request
) -> dict:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    track = _track_detail_queryset().filter(pk=track_id, project=project).first()
    if track is None:
        raise TrackNotFound("Track was not found.")
    return _track_detail_payload(track, actor, request)


@transaction.atomic
def update_track(
    *,
    actor: User,
    project_id: int,
    track_id: int,
    data: Mapping[str, Any],
    request,
) -> dict:
    project = _project_for_action(
        actor,
        project_id,
        policy.Action.EDIT_CONTENT,
        lock=True,
    )
    track = (
        MusicTrack.objects.select_for_update()
        .filter(pk=track_id, project=project)
        .first()
    )
    if track is None:
        raise TrackNotFound("Track was not found.")
    expected_version = data.get("version")
    if expected_version is not None and track.version != expected_version:
        raise VersionConflict(track.version)
    changed_fields = []
    for public_name, field_name in (
        ("title", "title"),
        ("author", "author"),
        ("tags", "tags"),
    ):
        if public_name in data:
            setattr(track, field_name, data[public_name])
            changed_fields.append(field_name)
    if "durationSeconds" in data or "duration_seconds" in data:
        track.duration_seconds = data.get(
            "durationSeconds", data.get("duration_seconds")
        )
        changed_fields.append("duration_seconds")
    if "activeVersionId" in data:
        _MusicAsset, _MusicGenerationJob, MusicTrackVersion, _MusicVariant = (
            _music_models()
        )
        active_version = MusicTrackVersion.objects.filter(
            pk=data["activeVersionId"], track=track
        ).first()
        if active_version is None:
            raise ValidationError(
                "Validation failed.",
                errors={"activeVersionId": ["Version does not belong to this track."]},
            )
        track.active_version = active_version
        changed_fields.append("active_version")
    if not changed_fields:
        return get_track(
            actor=actor, project_id=project.id, track_id=track.id, request=request
        )
    track.version += 1
    track.updated_by = actor
    changed_fields.extend(("version", "updated_by", "updated_at"))
    track.save(update_fields=list(dict.fromkeys(changed_fields)))
    record_activity(
        project,
        actor,
        "music_added",
        title=track.title,
        description="Music track updated",
        metadata={"track_id": track.id, "version": track.version},
    )
    return get_track(
        actor=actor,
        project_id=project.id,
        track_id=track.id,
        request=request,
    )


@transaction.atomic
def archive_track(
    *,
    actor: User,
    project_id: int,
    track_id: int,
    expected_version: int,
    request,
) -> dict:
    project = _project_for_action(
        actor,
        project_id,
        policy.Action.EDIT_CONTENT,
        lock=True,
    )
    track = (
        MusicTrack.objects.select_for_update()
        .filter(pk=track_id, project=project)
        .first()
    )
    if track is None:
        raise TrackNotFound("Track was not found.")
    if track.version != expected_version:
        raise VersionConflict(track.version)
    if track.archived_at is None:
        track.archived_at = timezone.now()
        track.version += 1
        track.updated_by = actor
        track.save(
            update_fields=("archived_at", "version", "updated_by", "updated_at")
        )
    return get_track(
        actor=actor,
        project_id=project.id,
        track_id=track.id,
        request=request,
    )


def get_assignments(*, actor: User, project_id: int, track_id: int) -> dict:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    track = MusicTrack.objects.filter(pk=track_id, project=project).first()
    if track is None:
        raise TrackNotFound("Track was not found.")
    assignments = (
        SceneMusic.objects.filter(track=track)
        .select_related("scene__location", "track_version")
        .prefetch_related(
            _scene_character_prefetch("scene__scene_characters")
        )
        .order_by("scene__order", "scene_id")
    )
    return {
        "trackId": track.id,
        "trackVersion": track.version,
        "items": [_assignment_payload(item) for item in assignments],
        "permissions": _permission_payload(actor, project),
    }


@transaction.atomic
def replace_assignments(
    *, actor: User, project_id: int, track_id: int, data: Mapping[str, Any]
) -> dict:
    project = _project_for_action(
        actor,
        project_id,
        policy.Action.EDIT_CONTENT,
        lock=True,
    )
    track = (
        MusicTrack.objects.select_for_update()
        .filter(pk=track_id, project=project)
        .first()
    )
    if track is None:
        raise TrackNotFound("Track was not found.")
    if track.version != data["expectedTrackVersion"]:
        raise VersionConflict(track.version)

    items = list(data["items"])
    scene_ids = {item["sceneId"] for item in items}
    version_ids = {item["trackVersionId"] for item in items}
    scenes = {
        scene.id: scene
        for scene in Scene.objects.filter(project=project, id__in=scene_ids)
    }
    if set(scenes) != scene_ids:
        raise SceneNotFound("One or more scenes were not found.")
    _MusicAsset, _MusicGenerationJob, MusicTrackVersion, _MusicVariant = (
        _music_models()
    )
    versions = {
        version.id: version
        for version in MusicTrackVersion.objects.filter(
            track=track, id__in=version_ids
        )
    }
    if set(versions) != version_ids:
        raise ValidationError(
            "Validation failed.",
            errors={
                "items": ["Every trackVersionId must belong to the target track."]
            },
        )
    SceneMusic.objects.filter(track=track).delete()
    SceneMusic.objects.bulk_create(
        [
            SceneMusic(
                scene=scenes[item["sceneId"]],
                track=track,
                track_version=versions[item["trackVersionId"]],
                start_time_seconds=item["startTimeSeconds"],
            )
            for item in items
        ]
    )
    track.version += 1
    track.updated_by = actor
    track.save(update_fields=("version", "updated_by", "updated_at"))
    record_activity(
        project,
        actor,
        "music_added",
        title=track.title,
        description="Music scene assignments updated",
        metadata={"track_id": track.id, "usage_count": len(items)},
    )
    return get_assignments(
        actor=actor,
        project_id=project.id,
        track_id=track.id,
    )


def _job_base_queryset():
    _MusicAsset, MusicGenerationJob, _MusicTrackVersion, MusicVariant = (
        _music_models()
    )
    variants = MusicVariant.objects.select_related(
        "asset__project", "applied_version"
    ).order_by("variant_index")
    return MusicGenerationJob.objects.select_related(
        "project",
        "target_track",
        "reference_asset__project",
        "retry_of",
    ).prefetch_related(
        Prefetch("variants", queryset=variants, to_attr="_music_variants")
    )


def _variant_payload(variant, request) -> dict:
    asset_payload = _asset_payload(variant.asset, request) or {}
    applied = getattr(variant, "applied_version", None)
    return {
        "variantId": str(variant.id),
        "index": variant.variant_index,
        "status": variant.status,
        "durationSeconds": asset_payload.get("durationSeconds"),
        "mimeType": asset_payload.get("mimeType"),
        "audioUrl": asset_payload.get("audioUrl"),
        "audioUrlExpiresAt": asset_payload.get("audioUrlExpiresAt"),
        "seed": variant.seed,
        "appliedTrackVersionId": str(applied.id) if applied is not None else None,
    }


def _job_error_payload(job) -> dict | None:
    if not job.error_code:
        return None
    detail = public_provider_error_detail(job.error_code)
    return {
        "code": job.error_code,
        "detail": detail or job.error_detail or "Music generation failed.",
        "retryable": job.error_retryable,
    }


def _job_payload(
    job,
    actor: User,
    request,
    *,
    include_variants: bool = True,
    permissions: dict | None = None,
) -> dict:
    effective_permissions = permissions or _permission_payload(actor, job.project)
    can_run_generation = bool(effective_permissions["canRunGeneration"])
    payload = {
        "jobId": str(job.id),
        "status": job.status,
        "stage": job.stage,
        "variantCount": job.variant_count,
        "brief": dict(job.brief or {}),
        "referenceAsset": _asset_payload(
            job.reference_asset,
            request,
            include_name=True,
        ),
        "targetTrackId": job.target_track_id,
        "retryOf": str(job.retry_of_id) if job.retry_of_id else None,
        "attempts": job.attempts,
        "canCancel": can_run_generation and job.status in CANCELLABLE_JOB_STATUSES,
        "canRetry": can_run_generation and job.status in RETRYABLE_JOB_STATUSES,
        "error": _job_error_payload(job),
        "createdAt": _iso_datetime(job.created_at),
        "completedAt": _iso_datetime(job.completed_at),
        "permissions": effective_permissions,
    }
    if include_variants:
        payload["variants"] = [
            _variant_payload(variant, request)
            for variant in getattr(job, "_music_variants", ())
        ]
    return payload


def _enqueue_payload(job, *, idempotent_replay: bool) -> dict:
    return {
        "jobId": str(job.id),
        "status": job.status,
        "stage": job.stage,
        "idempotentReplay": idempotent_replay,
        "pollAfterMs": 3000,
        "createdAt": _iso_datetime(job.created_at),
    }


def enqueue_job(
    *,
    actor: User,
    project_id: int,
    data: Mapping[str, Any],
    idempotency_key: str,
) -> dict:
    project = _project_for_action(actor, project_id, policy.Action.RUN_GENERATION)
    key = str(idempotency_key or "").strip()
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValidationError(
            "Validation failed.",
            errors={
                "Idempotency-Key": [
                    f"Must contain at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters."
                ]
            },
        )

    target_track = None
    target_track_id = data.get("targetTrackId")
    if target_track_id is not None:
        target_track = MusicTrack.objects.filter(
            pk=target_track_id, project=project
        ).first()
        if target_track is None:
            raise TrackNotFound("Target track was not found.")
    reference_asset = None
    reference_asset_id = data.get("referenceAssetId")
    if reference_asset_id is not None:
        MusicAsset, _MusicGenerationJob, _MusicTrackVersion, _MusicVariant = (
            _music_models()
        )
        reference_asset = MusicAsset.objects.filter(
            pk=reference_asset_id,
            project=project,
            asset_role="reference",
            rights_confirmed_at__isnull=False,
            verification_status="verified",
        ).first()
        if reference_asset is None:
            raise ReferenceNotFound("Reference asset was not found.")
    context = data["brief"].get("context") or {}
    if context.get("type") == "scene":
        if not Scene.objects.filter(
            pk=context.get("sceneId"), project=project
        ).exists():
            raise SceneNotFound("Brief scene was not found.")

    from w_craft_back.movie.music.lifecycle import enqueue_music_job
    from w_craft_back.movie.music.prompt_compiler import normalize_music_brief

    try:
        normalized_brief = normalize_music_brief(dict(data["brief"]))
        job, replay = enqueue_music_job(
            project=project,
            actor=actor,
            brief=normalized_brief,
            variant_count=int(data["variantCount"]),
            idempotency_key=key,
            target_track=target_track,
            reference_asset=reference_asset,
        )
    except MusicError:
        raise
    except Exception as error:
        adapted = _adapt_domain_error(error)
        if adapted is None:
            raise
        raise adapted from error
    return _enqueue_payload(job, idempotent_replay=replay)


def list_jobs(
    *,
    actor: User,
    project_id: int,
    request,
    limit: Any = 30,
    offset: Any = 0,
    status_filter: str = "",
) -> dict:
    project = _project_for_action(actor, project_id, policy.Action.VIEW)
    page_limit = _coerce_int(
        limit,
        default=30,
        minimum=1,
        maximum=MAX_LIBRARY_PAGE_SIZE,
        field="limit",
    )
    page_offset = _coerce_int(
        offset,
        default=0,
        minimum=0,
        maximum=2_147_483_647,
        field="offset",
    )
    queryset = _job_base_queryset().filter(project=project)
    if status_filter:
        allowed = TERMINAL_JOB_STATUSES | CANCELLABLE_JOB_STATUSES
        if status_filter not in allowed:
            raise ValidationError(
                "Validation failed.", errors={"status": ["Unknown job status."]}
            )
        queryset = queryset.filter(status=status_filter)
    queryset = queryset.order_by("-created_at", "-id")
    total = queryset.count()
    jobs = list(queryset[page_offset:page_offset + page_limit])
    permissions = _permission_payload(actor, project)
    return {
        "items": [
            _job_payload(job, actor, request, permissions=permissions)
            for job in jobs
        ],
        "page": {"limit": page_limit, "offset": page_offset, "total": total},
        "permissions": permissions,
    }


def _get_job_for_action(
    *, actor: User, project_id: int, job_id, action: policy.Action
):
    project = _project_for_action(actor, project_id, action)
    job = _job_base_queryset().filter(pk=job_id, project=project).first()
    if job is None:
        raise JobNotFound("Generation job was not found.")
    return job


def get_job(
    *, actor: User, project_id: int, job_id, request
) -> dict:
    job = _get_job_for_action(
        actor=actor,
        project_id=project_id,
        job_id=job_id,
        action=policy.Action.VIEW,
    )
    return _job_payload(job, actor, request)


def cancel_job(
    *, actor: User, project_id: int, job_id, request
) -> dict:
    job = _get_job_for_action(
        actor=actor,
        project_id=project_id,
        job_id=job_id,
        action=policy.Action.RUN_GENERATION,
    )
    from w_craft_back.movie.music.lifecycle import request_music_cancellation

    try:
        cancelled = request_music_cancellation(job)
    except Exception as error:
        adapted = _adapt_domain_error(error)
        if adapted is None:
            raise
        raise adapted from error
    if cancelled.status in TERMINAL_JOB_STATUSES:
        raise CannotCancel("A terminal generation job cannot be cancelled.")
    refreshed = _job_base_queryset().get(pk=job.pk)
    return _job_payload(refreshed, actor, request)


def retry_job(*, actor: User, project_id: int, job_id) -> dict:
    original = _get_job_for_action(
        actor=actor,
        project_id=project_id,
        job_id=job_id,
        action=policy.Action.RUN_GENERATION,
    )
    from w_craft_back.movie.music.lifecycle import retry_music_job

    try:
        retried = retry_music_job(original, actor=actor)
    except Exception as error:
        adapted = _adapt_domain_error(error)
        if adapted is None:
            raise
        raise adapted from error
    return _enqueue_payload(retried, idempotent_replay=False)


def _lyrics_snapshot(brief: Mapping[str, Any]) -> list[dict]:
    content = brief.get("content") or {}
    if content.get("mode") != "song":
        return []
    return [dict(section) for section in content.get("sections") or []]


@transaction.atomic
def apply_variant(
    *,
    actor: User,
    project_id: int,
    job_id,
    variant_id,
    data: Mapping[str, Any],
    request,
) -> tuple[dict, bool]:
    project = _project_for_action(
        actor,
        project_id,
        policy.Action.EDIT_CONTENT,
        lock=True,
    )
    _MusicAsset, MusicGenerationJob, MusicTrackVersion, MusicVariant = (
        _music_models()
    )
    job = MusicGenerationJob.objects.filter(pk=job_id, project=project).first()
    if job is None:
        raise JobNotFound("Generation job was not found.")
    variant = (
        MusicVariant.objects.select_for_update()
        .select_related("asset")
        .filter(pk=variant_id, job=job)
        .first()
    )
    if variant is None:
        raise VariantNotFound("Generation variant was not found.")
    existing_version = MusicTrackVersion.objects.select_related(
        "track", "asset"
    ).filter(source_variant=variant).first()
    if existing_version is not None:
        active = _version_payload(existing_version.track.active_version, request)
        return (
            {
                "trackId": existing_version.track_id,
                "trackVersion": existing_version.track.version,
                "activeVersion": active,
                "idempotentReplay": True,
            },
            False,
        )
    if job.status != "completed" or variant.status != "generated":
        raise ValidationError(
            "Only a completed generated variant can be applied."
        )

    target_track_id = (
        data["targetTrackId"]
        if "targetTrackId" in data
        else job.target_track_id
    )
    created_track = target_track_id is None
    if created_track:
        track = MusicTrack.objects.create(
            project=project,
            title=data["title"],
            author=data.get("author", ""),
            tags=list(data.get("tags") or []),
            source="generated",
            created_by=actor,
            updated_by=actor,
        )
        version_number = 1
    else:
        track = (
            MusicTrack.objects.select_for_update()
            .filter(pk=target_track_id, project=project)
            .first()
        )
        if track is None:
            raise TrackNotFound("Target track was not found.")
        expected = data.get("expectedTrackVersion")
        if expected is None or track.version != expected:
            raise VersionConflict(track.version)
        latest = (
            MusicTrackVersion.objects.filter(track=track)
            .order_by("-version_number")
            .values_list("version_number", flat=True)
            .first()
        )
        version_number = int(latest or 0) + 1
        track.title = data["title"]
        track.author = data.get("author", "")
        track.tags = list(data.get("tags") or [])
        track.version += 1
        track.updated_by = actor

    version = MusicTrackVersion.objects.create(
        track=track,
        version_number=version_number,
        asset=variant.asset,
        brief_snapshot=dict(job.brief or {}),
        lyrics_snapshot=_lyrics_snapshot(job.brief or {}),
        reference_asset=job.reference_asset,
        source_variant=variant,
        created_by=actor,
    )
    if created_track or data.get("makeActive", True):
        track.active_version = version
    if created_track:
        track.save(update_fields=("active_version", "updated_at"))
    else:
        fields = ["title", "author", "tags", "version", "updated_by", "updated_at"]
        if data.get("makeActive", True):
            fields.append("active_version")
        track.save(update_fields=fields)
    record_activity(
        project,
        actor,
        "music_added",
        title=track.title,
        description=f"Music version {version.version_number} applied",
        metadata={
            "track_id": track.id,
            "track_version_id": str(version.id),
            "source_variant_id": str(variant.id),
        },
    )
    return (
        {
            "trackId": track.id,
            "trackVersion": track.version,
            "activeVersion": _version_payload(track.active_version, request),
            "idempotentReplay": False,
        },
        created_track,
    )
