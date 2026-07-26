import hashlib
import logging
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Max

from w_craft_back.character_studio.models import (
    CharacterAsset,
    CharacterAssetStatus,
    CharacterAssetType,
)
from w_craft_back.character_studio.repositories.repositories import AssetRepository
from w_craft_back.character_studio.services.errors import NotFoundError, ValidationError
from w_craft_back.character_studio.services.permissions import get_project_for_action
from w_craft_back.movie.project.policy import Action


logger = logging.getLogger(__name__)


# Reference asset_types. Kept here so that asset_service is the single source
# of truth for "what is a reference asset?" used by readiness/upload/version.
REFERENCE_ASSET_TYPES = (
    CharacterAssetType.PORTRAIT,
    CharacterAssetType.FULL_BODY,
    CharacterAssetType.THREE_QUARTER,
    CharacterAssetType.PROFILE,
    CharacterAssetType.BACK_VIEW,
    CharacterAssetType.EMOTIONS_SHEET,
    CharacterAssetType.POSES_SHEET,
    CharacterAssetType.OUTFIT_DETAILS,
    CharacterAssetType.REFERENCE_SHEET,
)

# UI types in stable order. character_sheet is the UI alias for reference_sheet.
REFERENCE_UI_ORDER = (
    "portrait",
    "full_body",
    "three_quarter",
    "profile",
    "back_view",
    "emotions",
    "poses",
    "outfit_details",
    "character_sheet",
)

REFERENCE_UI_TO_ASSET_TYPE = {
    "portrait": CharacterAssetType.PORTRAIT,
    "full_body": CharacterAssetType.FULL_BODY,
    "three_quarter": CharacterAssetType.THREE_QUARTER,
    "profile": CharacterAssetType.PROFILE,
    "back_view": CharacterAssetType.BACK_VIEW,
    "emotions": CharacterAssetType.EMOTIONS_SHEET,
    "poses": CharacterAssetType.POSES_SHEET,
    "outfit_details": CharacterAssetType.OUTFIT_DETAILS,
    "character_sheet": CharacterAssetType.REFERENCE_SHEET,
}

ASSET_TYPE_TO_REFERENCE_UI = {value: key for key, value in REFERENCE_UI_TO_ASSET_TYPE.items()}

# Required reference UI types for the proceed-to-3D gate. Either profile OR
# three_quarter is acceptable (covered explicitly in compute_readiness).
REQUIRED_REFERENCE_UI_TYPES = ("portrait", "full_body", "back_view")

ALLOWED_UPLOAD_MIME = {"image/jpeg", "image/png", "image/webp"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MIME_TO_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class CharacterAssetService:
    def __init__(self, repository=None):
        self.assets = repository or AssetRepository()

    def save_asset(self, actor, action, character, asset_type, **payload):
        # Auto-increment version for reference asset_types so each regeneration
        # / correction / upload becomes a new versioned row instead of clobbering
        # the previous one. Non-reference asset_types keep the default version=1.
        # Wrap in atomic + select_for_update on the character row so concurrent
        # uploads serialize and cannot produce duplicate versions.
        from w_craft_back.character_studio.models import StudioCharacter
        if action is not Action.RUN_GENERATION:
            raise ValueError(
                f"Generated asset mutation requires run_generation, received {action.value}"
            )
        get_project_for_action(actor, character.project_id, action)
        with transaction.atomic():
            if asset_type in REFERENCE_ASSET_TYPES and "version" not in payload:
                # Lock the parent character to serialize concurrent writers.
                StudioCharacter.objects.select_for_update().filter(pk=character.pk).first()
                payload["version"] = self._next_version(character, asset_type)
            payload.setdefault("status", CharacterAssetStatus.READY)
            return self.assets.create(
                character=character,
                project=character.project,
                user=actor,
                asset_type=asset_type,
                **payload,
            )

    def get_asset(self, asset_id):
        return self.assets.get(asset_id=asset_id)

    def delete_asset(self, asset):
        self.assets.delete(asset)

    def mark_as_primary(self, asset):
        return self.assets.mark_as_primary(asset)

    # --- References stage --------------------------------------------------

    def _next_version(self, character, asset_type):
        agg = CharacterAsset.objects.filter(
            character=character,
            asset_type=asset_type,
        ).aggregate(Max("version"))
        return (agg["version__max"] or 0) + 1

    def latest_ready_by_reference_type(self, character):
        """Return {asset_type: latest CharacterAsset} for ready reference assets."""
        result = {}
        queryset = (
            CharacterAsset.objects.filter(
                character=character,
                asset_type__in=REFERENCE_ASSET_TYPES,
                status=CharacterAssetStatus.READY,
            )
            .order_by("asset_type", "-version", "-created_at")
        )
        seen = set()
        for asset in queryset:
            if asset.asset_type in seen:
                continue
            seen.add(asset.asset_type)
            result[asset.asset_type] = asset
        return result

    @transaction.atomic
    def make_primary_reference(self, character, reference_id):
        try:
            asset = character.assets.get(asset_id=reference_id)
        except CharacterAsset.DoesNotExist as exc:
            raise NotFoundError("Reference asset not found.") from exc
        if asset.asset_type not in REFERENCE_ASSET_TYPES:
            raise ValidationError("Asset is not a reference and cannot be primary.")
        if asset.status != CharacterAssetStatus.READY:
            raise ValidationError("Cannot make a non-ready reference primary.")
        CharacterAsset.objects.filter(character=character).update(is_primary=False)
        asset.is_primary = True
        asset.save(update_fields=["is_primary", "updated_at"])
        return asset

    @transaction.atomic
    def upload_reference(self, character, user, ui_reference_type, uploaded_file, replace_current=False):
        if ui_reference_type not in REFERENCE_UI_TO_ASSET_TYPE:
            raise ValidationError(f"Unknown reference_type: {ui_reference_type}.")
        asset_type = REFERENCE_UI_TO_ASSET_TYPE[ui_reference_type]

        if not uploaded_file:
            raise ValidationError("No file provided.")
        content_type = getattr(uploaded_file, "content_type", "") or ""
        if content_type not in ALLOWED_UPLOAD_MIME:
            raise ValidationError("Only jpg, png and webp are supported.")
        size = getattr(uploaded_file, "size", 0) or 0
        if size > MAX_UPLOAD_BYTES:
            raise ValidationError("File exceeds 10 MB limit.")

        version = self._next_version(character, asset_type)
        ext = MIME_TO_EXT[content_type]
        # Filename derived from a fresh uuid + version, NEVER from the user's
        # filename (which we do not trust).
        filename = f"v{version}_{uuid.uuid4().hex}.{ext}"
        rel_path = (
            f"character-studio/characters/{character.character_id}/references/"
            f"{ui_reference_type}/{filename}"
        )
        abs_path = Path(settings.MEDIA_ROOT) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        with abs_path.open("wb") as fh:
            for chunk in uploaded_file.chunks():
                hasher.update(chunk)
                fh.write(chunk)

        media_url = getattr(settings, "MEDIA_URL", "/media/")
        if not media_url.endswith("/"):
            media_url += "/"
        image_url = f"{media_url}{rel_path}"

        if replace_current:
            CharacterAsset.objects.filter(
                character=character,
                asset_type=asset_type,
                status=CharacterAssetStatus.GENERATING,
            ).update(status=CharacterAssetStatus.FAILED, error_message="Replaced by upload.")

        asset = CharacterAsset.objects.create(
            character=character,
            project=character.project,
            user=user,
            asset_type=asset_type,
            image_url=image_url,
            storage_path=rel_path,
            mime_type=content_type,
            source="uploaded",
            version=version,
            status=CharacterAssetStatus.READY,
            metadata={"uploaded": True, "sha256": hasher.hexdigest()},
        )
        return asset

    @transaction.atomic
    def save_uploaded_source_reference(self, character, user, uploaded_file):
        """Save a user-uploaded source image to seed reference-based generation.

        Unlike :meth:`upload_reference`, this does not target a specific UI
        reference slot (portrait / full_body / ...). It records the original
        reference picture the user provided when creating the character.
        """
        if not uploaded_file:
            raise ValidationError("No file provided.")
        content_type = getattr(uploaded_file, "content_type", "") or ""
        if content_type not in ALLOWED_UPLOAD_MIME:
            raise ValidationError("Only jpg, png and webp are supported.")
        size = getattr(uploaded_file, "size", 0) or 0
        if size > MAX_UPLOAD_BYTES:
            raise ValidationError("File exceeds 10 MB limit.")

        ext = MIME_TO_EXT[content_type]
        filename = f"{uuid.uuid4().hex}.{ext}"
        rel_path = (
            f"character-studio/characters/{character.character_id}/source/{filename}"
        )
        abs_path = Path(settings.MEDIA_ROOT) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        with abs_path.open("wb") as fh:
            for chunk in uploaded_file.chunks():
                hasher.update(chunk)
                fh.write(chunk)

        media_url = getattr(settings, "MEDIA_URL", "/media/")
        if not media_url.endswith("/"):
            media_url += "/"
        image_url = f"{media_url}{rel_path}"

        asset = CharacterAsset.objects.create(
            character=character,
            project=character.project,
            user=user,
            asset_type=CharacterAssetType.UPLOADED_REFERENCE,
            image_url=image_url,
            storage_path=rel_path,
            mime_type=content_type,
            source="uploaded",
            status=CharacterAssetStatus.READY,
            metadata={"uploaded": True, "sha256": hasher.hexdigest(), "role": "source"},
        )
        return asset

    def compute_readiness(self, character):
        """Compute the readiness summary used by the References screen.

        Technical requirements (portrait + full_body + (profile|three_quarter)
        + back_view, all ready, no in-flight job, no failed required) BLOCK
        proceed-to-3D. Subjective user-checklist items (appearance_stable,
        face_matches_base, outfit_readable, suitable_for_3d) are returned in
        the response but do NOT block — they only show as warnings. This is
        intentional: we don't want to stall a real production flow on a user
        forgetting to tick a self-affirmation box.
        """
        latest = self.latest_ready_by_reference_type(character)

        portrait_ready = CharacterAssetType.PORTRAIT in latest
        full_body_ready = CharacterAssetType.FULL_BODY in latest
        back_view_ready = CharacterAssetType.BACK_VIEW in latest
        profile_or_3q_ready = (
            CharacterAssetType.PROFILE in latest
            or CharacterAssetType.THREE_QUARTER in latest
        )
        front_side_back_ready = full_body_ready and back_view_ready and profile_or_3q_ready

        # Generating assets on required types — block until they finish.
        generating_on_required = CharacterAsset.objects.filter(
            character=character,
            asset_type__in=(
                CharacterAssetType.PORTRAIT,
                CharacterAssetType.FULL_BODY,
                CharacterAssetType.PROFILE,
                CharacterAssetType.THREE_QUARTER,
                CharacterAssetType.BACK_VIEW,
            ),
            status=CharacterAssetStatus.GENERATING,
        ).exists()

        # Subjective user checklist — defaults False; user-editable through PATCH.
        user_state = dict(character.references_state or {})
        appearance_stable = bool(user_state.get("appearance_stable", False))
        face_matches_base = bool(user_state.get("face_matches_base", False))
        outfit_readable = bool(user_state.get("outfit_readable", False))
        suitable_for_3d = bool(user_state.get("suitable_for_3d", False))

        blockers = []
        if not portrait_ready:
            blockers.append("missing_portrait")
        if not full_body_ready:
            blockers.append("missing_full_body")
        if not profile_or_3q_ready:
            blockers.append("missing_profile_or_three_quarter")
        if not back_view_ready:
            blockers.append("missing_back_view")
        if generating_on_required:
            blockers.append("generation_in_progress")

        can_proceed = not blockers

        return {
            "can_proceed": can_proceed,
            "blockers": blockers,
            "checklist": {
                "appearance_stable": appearance_stable,
                "face_matches_base": face_matches_base,
                "outfit_readable": outfit_readable,
                "full_body_ready": full_body_ready,
                "front_side_back_ready": front_side_back_ready,
                "suitable_for_3d": suitable_for_3d,
            },
            "latest_ready_by_type": latest,
        }

