import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.models import Project


class CharacterType(models.TextChoices):
    HUMAN = "human", "Human"
    ANIMAL = "animal", "Animal"
    CREATURE = "creature", "Creature"
    ROBOT = "robot", "Robot"
    OBJECT = "object", "Object"
    OTHER = "other", "Other"


class CharacterRole(models.TextChoices):
    MAIN = "main", "Главный герой"
    SECONDARY = "secondary", "Второстепенный персонаж"
    ANTAGONIST = "antagonist", "Антагонист"
    EPISODIC = "episodic", "Эпизодический"
    CAMEO = "cameo", "Камео"


class CharacterStatus(models.TextChoices):
    # A character that has been started but not yet confirmed by the user.
    # We persist drafts because generation needs a character_id to attach
    # variants to, but drafts must NOT show up in the gallery or tree.
    DRAFT = "draft", "Draft"
    # A character the user has confirmed (applied a variant or otherwise
    # explicitly saved). This is the default "visible" state.
    ACTIVE = "active", "Active"
    REFERENCES_LOCKED = "references_locked", "References locked"


# Statuses that should appear in normal user-facing lists (gallery + tree).
# Drafts are intentionally excluded — they're unfinished creation attempts.
VISIBLE_CHARACTER_STATUSES = (
    CharacterStatus.ACTIVE,
    CharacterStatus.REFERENCES_LOCKED,
)


def _character_link_errors(
    instance: models.Model,
    character_id,
    field_names: tuple[str, ...],
) -> dict[str, str]:
    """Return validation errors for links outside one character aggregate."""
    errors = {}
    for field_name in field_names:
        if not getattr(instance, f"{field_name}_id"):
            continue
        linked = getattr(instance, field_name)
        if linked.character_id != character_id:
            errors[field_name] = "Related object must belong to the same character."
    return errors


class CharacterAssetType(models.TextChoices):
    UPLOADED_REFERENCE = "uploaded_reference", "Uploaded reference"
    INITIAL_VARIANT = "initial_variant", "Initial variant"
    EDIT_VARIANT = "edit_variant", "Edit variant"
    CANONICAL_REFERENCE = "canonical_reference", "Canonical reference"
    PORTRAIT = "portrait", "Portrait"
    FULL_BODY = "full_body", "Full body"
    SCENE = "scene", "Scene"
    REFERENCE_SHEET = "reference_sheet", "Reference sheet"
    FACE_CLOSEUP = "face_closeup", "Face closeup"
    FRONT_VIEW = "front_view", "Front view"
    SIDE_VIEW = "side_view", "Side view"
    THREE_QUARTER = "three_quarter", "Three-quarter view"
    PROFILE = "profile", "Profile view"
    BACK_VIEW = "back_view", "Back view"
    EMOTIONS_SHEET = "emotions_sheet", "Emotions sheet"
    POSES_SHEET = "poses_sheet", "Poses sheet"
    OUTFIT_DETAILS = "outfit_details", "Outfit details"
    EXPRESSION = "expression", "Expression"
    OUTFIT_REFERENCE = "outfit_reference", "Outfit reference"
    CLOTHING_REFERENCE = "clothing_reference", "Clothing reference"
    THUMBNAIL = "thumbnail", "Thumbnail"
    MODEL_3D = "model_3d", "3D model"


class CharacterAssetStatus(models.TextChoices):
    GENERATING = "generating", "Generating"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class GenerationJobType(models.TextChoices):
    INITIAL_VARIANTS = "initial_variants", "Initial variants"
    EDIT_VARIANTS = "edit_variants", "Edit variants"
    OUTFIT_VARIANTS = "outfit_variants", "Outfit variants"
    EXPRESSION_VARIANTS = "expression_variants", "Expression variants"
    CHARACTER_SHEET = "character_sheet", "Character sheet"
    REFERENCE_EXTRACTION = "reference_extraction", "Reference extraction"
    REFERENCE_VARIANTS = "reference_variants", "Reference-based variants"
    MODEL3D_RECONSTRUCTION = "model3d_reconstruction", "3D reconstruction"


class GenerationJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    CANCELLATION_REQUESTED = "cancellation_requested", "Cancellation requested"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class CharacterRegion(models.TextChoices):
    FACE = "face", "Face"
    HAIR = "hair", "Hair"
    BODY = "body", "Body"
    OUTFIT = "outfit", "Outfit"
    STYLE = "style", "Style"
    FULL_CHARACTER = "full_character", "Full character"


class CharacterImageType(models.TextChoices):
    PORTRAIT = "portrait", "Portrait"
    FULL_BODY = "full_body", "Full body"
    SCENE = "scene", "Scene"
    REFERENCE_SHEET = "reference_sheet", "Reference sheet"
    THREE_QUARTER = "three_quarter", "Three-quarter view"
    PROFILE = "profile", "Profile view"
    BACK_VIEW = "back_view", "Back view"
    EMOTIONS = "emotions", "Emotions"
    POSES = "poses", "Poses"
    OUTFIT_DETAILS = "outfit_details", "Outfit details"


class VariantStatus(models.TextChoices):
    GENERATED = "generated", "Generated"
    APPLIED = "applied", "Applied"
    DISCARDED = "discarded", "Discarded"


class RevisionChangeType(models.TextChoices):
    INITIAL_CREATE = "initial_create", "Initial create"
    APPLY_VARIANT = "apply_variant", "Apply variant"
    MANUAL_UPDATE = "manual_update", "Manual update"
    OUTFIT_CHANGE = "outfit_change", "Outfit change"
    IDENTITY_LOCK = "identity_lock", "Identity lock"
    RESTORE_REVISION = "restore_revision", "Restore revision"
    VERSION_CREATE = "version_create", "Version create"


class ExpressionType(models.TextChoices):
    NEUTRAL = "neutral", "Neutral"
    HAPPY = "happy", "Happy"
    SAD = "sad", "Sad"
    ANGRY = "angry", "Angry"
    SCARED = "scared", "Scared"
    SURPRISED = "surprised", "Surprised"
    TIRED = "tired", "Tired"
    SARCASTIC = "sarcastic", "Sarcastic"
    CONFUSED = "confused", "Confused"
    CRYING = "crying", "Crying"
    SMIRKING = "smirking", "Smirking"


class StudioCharacter(models.Model):
    character_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="studio_characters")
    user = models.ForeignKey(
        UserKey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="studio_characters",
    )
    creation_idempotency_key = models.CharField(
        max_length=128,
        blank=True,
        default="",
    )
    creation_request_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )
    name = models.CharField(max_length=255)
    character_type = models.CharField(max_length=32, choices=CharacterType.choices, default=CharacterType.HUMAN)
    role = models.CharField(max_length=100, blank=True, default="", choices=CharacterRole.choices)
    short_description = models.TextField(blank=True, default="")
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    lifecycle_stage = models.CharField(max_length=128, blank=True, default="")
    gender = models.CharField(max_length=100, blank=True, default="")
    species = models.CharField(max_length=100, default="human")
    visual_style = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=CharacterStatus.choices,
        default=CharacterStatus.DRAFT,
        db_index=True,
    )
    identity_locked = models.BooleanField(default=False)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(
        UserKey,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="locked_studio_characters",
    )
    active_appearance = models.ForeignKey(
        "CharacterAppearance",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    active_outfit = models.ForeignKey(
        "CharacterOutfit",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    active_version = models.ForeignKey(
        "CharacterVersion",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    current_revision = models.ForeignKey(
        "CharacterRevision",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    canonical_reference_image = models.ForeignKey(
        "CharacterAsset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="canonical_for_characters",
    )
    personality = models.JSONField(default=dict, blank=True)
    speech_style = models.TextField(blank=True, default="")
    backstory = models.TextField(blank=True, default="")
    clothing_source = models.CharField(max_length=20, blank=True, default="text")
    clothing_description = models.TextField(blank=True, default="")
    # User-editable references checklist (subjective items only). Auto-derived
    # items (full_body_ready, front_side_back_ready) are computed at request time.
    references_state = models.JSONField(default=dict, blank=True)
    # Parametric state of the 3D editor stage: {zone_id: {param_id: value}}.
    # Leaf values are numbers in [-1, 1], short strings (color hex / preset
    # ids) or booleans — validated in services/model3d_service.py.
    model3d_params = models.JSONField(default=dict, blank=True)
    # Whether autofit-from-references has already run for the 3D stage. The
    # editor seeds parameters from the portrait automatically on first open;
    # this flag stops it from overwriting the user's manual edits on later
    # opens, even if they reset everything back to defaults.
    model3d_autofit_done = models.BooleanField(default=False)
    # Incremented when a new autofit profile has been applied. This lets the
    # editor upgrade old sparse fits without overwriting manual parameters.
    model3d_autofit_version = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "characters"
        indexes = [
            models.Index(fields=["project"], name="characters_project_95fdda_idx"),
            models.Index(fields=["user"], name="characters_user_id_6db2fe_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(age__isnull=True) | (Q(age__gte=0) & Q(age__lte=130)),
                name="chk_studio_character_age_range",
            ),
            models.CheckConstraint(
                check=Q(character_type__in=CharacterType.values),
                name="chk_studio_character_type",
            ),
            models.UniqueConstraint(
                fields=["project", "user", "creation_idempotency_key"],
                condition=~Q(creation_idempotency_key=""),
                name="uniq_char_create_idempotency",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors = _character_link_errors(
            self,
            self.character_id,
            (
                "active_appearance",
                "active_outfit",
                "active_version",
                "current_revision",
                "canonical_reference_image",
            ),
        )
        if self.canonical_reference_image_id and self.project_id:
            if self.canonical_reference_image.project_id != self.project_id:
                errors["canonical_reference_image"] = (
                    "Canonical reference image must belong to the character project."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class CharacterAppearance(models.Model):
    appearance_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="appearances")
    face_shape = models.CharField(max_length=100, blank=True, default="")
    skin_tone = models.CharField(max_length=100, blank=True, default="")
    eye_shape = models.CharField(max_length=100, blank=True, default="")
    eye_color = models.CharField(max_length=100, blank=True, default="")
    eyebrow_shape = models.CharField(max_length=100, blank=True, default="")
    nose_shape = models.CharField(max_length=100, blank=True, default="")
    lips_shape = models.CharField(max_length=100, blank=True, default="")
    jawline = models.CharField(max_length=100, blank=True, default="")
    hair_length = models.CharField(max_length=100, blank=True, default="")
    hair_style = models.CharField(max_length=100, blank=True, default="")
    hair_color = models.CharField(max_length=100, blank=True, default="")
    hair_details = models.JSONField(default=dict, blank=True)
    height = models.CharField(max_length=100, blank=True, default="")
    # Numeric height in centimeters. Drives the height ruler in the full-body
    # editor and is fed into the generation prompt. The legacy `height` enum
    # field above is kept for backwards-compatibility but no longer used.
    height_cm = models.PositiveSmallIntegerField(null=True, blank=True)
    body_type = models.CharField(max_length=100, blank=True, default="")
    body_structure = models.CharField(max_length=128, blank=True, default="")
    surface_material = models.CharField(max_length=128, blank=True, default="")
    special_features = models.TextField(blank=True, default="")
    posture = models.CharField(max_length=100, blank=True, default="")
    distinctive_features = models.JSONField(default=list, blank=True)
    appearance_prompt = models.TextField(blank=True, default="")
    negative_prompt = models.TextField(blank=True, default="")
    source_type = models.CharField(max_length=100, blank=True, default="")
    source_description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "character_appearances"
        indexes = [models.Index(fields=["character"], name="character_a_charact_7741ba_idx")]


class CharacterOutfit(models.Model):
    outfit_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="outfits")
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    style = models.CharField(max_length=100, blank=True, default="")
    color_palette = models.JSONField(default=list, blank=True)
    layers = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)
    reference_image = models.ForeignKey(
        "CharacterAsset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="outfit_references",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "character_outfits"
        indexes = [models.Index(fields=["character"], name="character_o_charact_a255ad_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["character"],
                condition=Q(is_default=True, archived_at__isnull=True),
                name="uniq_default_active_outfit",
            )
        ]

    def clean(self) -> None:
        super().clean()
        errors = _character_link_errors(
            self,
            self.character_id,
            ("reference_image",),
        )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class CharacterVersion(models.Model):
    version_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="versions")
    version_name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    appearance = models.ForeignKey(CharacterAppearance, null=True, blank=True, on_delete=models.SET_NULL)
    outfit = models.ForeignKey(CharacterOutfit, null=True, blank=True, on_delete=models.SET_NULL)
    reference_image = models.ForeignKey("CharacterAsset", null=True, blank=True, on_delete=models.SET_NULL)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "character_versions"
        indexes = [models.Index(fields=["character"], name="character_v_charact_4f112f_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["character"],
                condition=Q(is_default=True),
                name="uniq_default_version",
            ),
            models.UniqueConstraint(
                fields=["character"],
                condition=Q(is_active=True),
                name="uniq_active_version",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors = _character_link_errors(
            self,
            self.character_id,
            ("appearance", "outfit", "reference_image"),
        )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class CharacterAsset(models.Model):
    asset_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="assets")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="character_assets")
    user = models.ForeignKey(
        UserKey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="character_assets",
    )
    asset_type = models.CharField(max_length=64, choices=CharacterAssetType.choices)
    image_url = models.TextField(blank=True, default="")
    storage_path = models.TextField(blank=True, default="")
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    mime_type = models.CharField(max_length=100, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    is_canonical = models.BooleanField(default=False)
    source = models.CharField(max_length=100, blank=True, default="")
    source_job_id = models.UUIDField(null=True, blank=True)
    source_variant_id = models.UUIDField(null=True, blank=True)
    generation_prompt = models.TextField(blank=True, default="")
    negative_prompt = models.TextField(blank=True, default="")
    correction_prompt = models.TextField(blank=True, default="")
    model_name = models.CharField(max_length=100, blank=True, default="")
    model_version = models.CharField(max_length=100, blank=True, default="")
    seed = models.BigIntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    safety_status = models.CharField(max_length=100, blank=True, default="unchecked")
    # Lifecycle status of this asset row. Existing rows default to READY via
    # data migration; new rows representing in-progress generations are created
    # with GENERATING and updated to READY/FAILED when the job terminates.
    status = models.CharField(
        max_length=32,
        choices=CharacterAssetStatus.choices,
        default=CharacterAssetStatus.READY,
        db_index=True,
    )
    version = models.PositiveIntegerField(default=1)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "character_assets"
        indexes = [
            models.Index(fields=["character"], name="character_a_charact_0d1067_idx"),
            models.Index(fields=["project"], name="character_a_project_d415f6_idx"),
            models.Index(fields=["asset_type"], name="character_a_asset_t_07aa97_idx"),
            models.Index(fields=["character", "asset_type", "status"], name="character_a_char_at_st_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["character"],
                condition=Q(is_primary=True),
                name="uniq_primary_asset",
            ),
            models.UniqueConstraint(
                fields=["character"],
                condition=Q(is_canonical=True),
                name="uniq_canonical_asset",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.character_id and self.project_id:
            if self.character.project_id != self.project_id:
                raise ValidationError(
                    {"character": "Character must belong to the asset project."}
                )

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class CharacterImage(models.Model):
    image_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="images")
    asset = models.ForeignKey(
        CharacterAsset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="character_images",
    )
    image_type = models.CharField(max_length=32, choices=CharacterImageType.choices)
    image_url = models.TextField(blank=True, default="")
    storage_path = models.TextField(blank=True, default="")
    prompt = models.TextField(blank=True, default="")
    seed = models.BigIntegerField(null=True, blank=True)
    generation_params = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "character_images"
        indexes = [
            models.Index(fields=["character"], name="character_i_charact_72dd68_idx"),
            models.Index(fields=["image_type"], name="character_i_type_055e38_idx"),
            models.Index(fields=["character", "image_type"], name="character_i_char_ty_6ce70d_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["character", "image_type"],
                condition=Q(is_active=True),
                name="uniq_active_character_image_type",
            ),
            models.CheckConstraint(
                check=Q(image_type__in=CharacterImageType.values),
                name="chk_character_image_type",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors = _character_link_errors(
            self,
            self.character_id,
            ("asset",),
        )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class CharacterGenerationJob(models.Model):
    job_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="generation_jobs")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="character_generation_jobs")
    user = models.ForeignKey(
        UserKey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="character_generation_jobs",
    )
    actor = models.ForeignKey(
        UserKey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_character_generation_jobs",
    )
    job_type = models.CharField(max_length=64, choices=GenerationJobType.choices)
    status = models.CharField(max_length=32, choices=GenerationJobStatus.choices, default=GenerationJobStatus.QUEUED)
    region = models.CharField(max_length=32, choices=CharacterRegion.choices, default=CharacterRegion.FULL_CHARACTER)
    variant_count = models.PositiveSmallIntegerField(default=4)
    request_payload = models.JSONField(default=dict, blank=True)
    request_hash = models.CharField(max_length=64, blank=True, default="")
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    compiled_prompt = models.TextField(blank=True, default="")
    negative_prompt = models.TextField(blank=True, default="")
    edit_instruction = models.TextField(blank=True, default="")
    preserve_options = models.JSONField(default=dict, blank=True)
    compiled_metadata = models.JSONField(default=dict, blank=True)
    provider = models.CharField(max_length=255, blank=True, default="mock")
    provider_snapshot = models.JSONField(default=dict, blank=True)
    provider_operation = models.CharField(
        max_length=32,
        blank=True,
        default="generate",
    )
    model_name = models.CharField(max_length=255, blank=True, default="")
    model_version = models.CharField(max_length=255, blank=True, default="")
    progress = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    error_code = models.CharField(max_length=100, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    timeout_seconds = models.PositiveIntegerField(default=120)
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
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "character_generation_jobs"
        indexes = [
            models.Index(fields=["character"], name="character_g_charact_18486e_idx"),
            models.Index(fields=["project"], name="character_g_project_a65b63_idx"),
            models.Index(fields=["status"], name="character_g_status_a1b6e0_idx"),
            models.Index(
                fields=["status", "lease_expires_at"],
                name="char_job_status_lease_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(progress__gte=0) & Q(progress__lte=100),
                name="chk_generation_progress_range",
            ),
            models.CheckConstraint(
                check=Q(variant_count__in=[1, 2, 4]),
                name="chk_generation_variant_count",
            ),
            models.CheckConstraint(
                check=Q(attempts__lte=models.F("max_attempts")),
                name="chk_generation_attempts",
            ),
            models.UniqueConstraint(
                fields=["project", "actor", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="uniq_char_job_idempotency",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.character_id and self.project_id:
            if self.character.project_id != self.project_id:
                raise ValidationError(
                    {"character": "Character must belong to the generation job project."}
                )

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class CharacterGenerationGuard(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "character_generation_guards"


class CharacterVariant(models.Model):
    variant_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(CharacterGenerationJob, on_delete=models.CASCADE, related_name="variants")
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="variants")
    asset = models.ForeignKey(CharacterAsset, null=True, blank=True, on_delete=models.SET_NULL, related_name="variants")
    variant_index = models.PositiveSmallIntegerField()
    region = models.CharField(max_length=32, choices=CharacterRegion.choices)
    controls_snapshot = models.JSONField(default=dict, blank=True)
    appearance_snapshot = models.JSONField(default=dict, blank=True)
    image_url = models.TextField(blank=True, default="")
    prompt = models.TextField(blank=True, default="")
    negative_prompt = models.TextField(blank=True, default="")
    seed = models.BigIntegerField(null=True, blank=True)
    model_name = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=32, choices=VariantStatus.choices, default=VariantStatus.GENERATED)
    applied = models.BooleanField(default=False)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "character_variants"
        indexes = [
            models.Index(fields=["job"], name="character_v_job_id_e5472d_idx"),
            models.Index(fields=["character"], name="character_v_charact_e045f1_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "variant_index"],
                name="uniq_variant_index_per_job",
            )
        ]

    def clean(self) -> None:
        super().clean()
        errors = _character_link_errors(
            self,
            self.character_id,
            ("job", "asset"),
        )
        if self.job_id and self.character_id:
            if self.job.project_id != self.character.project_id:
                errors["job"] = "Job must belong to the character project."
        if self.asset_id and self.character_id:
            if self.asset.project_id != self.character.project_id:
                errors["asset"] = "Asset must belong to the character project."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class CharacterRevision(models.Model):
    revision_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="revisions")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="character_revisions")
    user = models.ForeignKey(
        UserKey,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="character_revisions",
    )
    revision_number = models.PositiveIntegerField()
    source_variant = models.ForeignKey(CharacterVariant, null=True, blank=True, on_delete=models.SET_NULL)
    source_job = models.ForeignKey(CharacterGenerationJob, null=True, blank=True, on_delete=models.SET_NULL)
    reference_image = models.ForeignKey(CharacterAsset, null=True, blank=True, on_delete=models.SET_NULL)
    appearance = models.ForeignKey(CharacterAppearance, null=True, blank=True, on_delete=models.SET_NULL)
    outfit = models.ForeignKey(CharacterOutfit, null=True, blank=True, on_delete=models.SET_NULL)
    version = models.ForeignKey(CharacterVersion, null=True, blank=True, on_delete=models.SET_NULL)
    change_type = models.CharField(max_length=64, choices=RevisionChangeType.choices)
    changed_region = models.CharField(max_length=32, choices=CharacterRegion.choices, blank=True, default="")
    change_summary = models.TextField(blank=True, default="")
    text_refinement = models.TextField(blank=True, default="")
    before_snapshot = models.JSONField(default=dict, blank=True)
    after_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "character_revisions"
        indexes = [models.Index(fields=["character"], name="character_r_charact_222f6e_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["character", "revision_number"],
                name="uniq_character_revision_number",
            )
        ]

    def clean(self) -> None:
        super().clean()
        errors = _character_link_errors(
            self,
            self.character_id,
            (
                "source_variant",
                "source_job",
                "reference_image",
                "appearance",
                "outfit",
                "version",
            ),
        )
        if self.character_id and self.project_id:
            if self.character.project_id != self.project_id:
                errors["character"] = "Character must belong to the revision project."
        if self.source_variant_id and self.source_job_id:
            if self.source_variant.job_id != self.source_job_id:
                errors["source_variant"] = (
                    "Source variant must belong to the revision source job."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class CharacterExpression(models.Model):
    expression_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="expressions")
    expression_type = models.CharField(max_length=32, choices=ExpressionType.choices)
    description = models.TextField(blank=True, default="")
    asset = models.ForeignKey(CharacterAsset, null=True, blank=True, on_delete=models.SET_NULL)
    prompt = models.TextField(blank=True, default="")
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "character_expressions"
        indexes = [models.Index(fields=["character"], name="character_e_charact_44bd33_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["character", "expression_type"],
                name="uniq_expression_type",
            ),
            models.UniqueConstraint(
                fields=["character"],
                condition=Q(is_default=True),
                name="uniq_default_expression",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors = _character_link_errors(
            self,
            self.character_id,
            ("asset",),
        )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class CharacterRelationship(models.Model):
    relationship_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="character_relationships")
    source_character = models.ForeignKey(
        StudioCharacter,
        on_delete=models.CASCADE,
        related_name="outgoing_relationships",
    )
    target_character = models.ForeignKey(
        StudioCharacter,
        on_delete=models.CASCADE,
        related_name="incoming_relationships",
    )
    relation_type = models.CharField(max_length=100)
    dynamic_description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "character_relationships"
        indexes = [
            models.Index(fields=["project"], name="character_r_project_9c31c7_idx"),
            models.Index(fields=["source_character"], name="character_r_source__a745ef_idx"),
            models.Index(fields=["target_character"], name="character_r_target__097a76_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "source_character", "target_character", "relation_type"],
                name="uniq_character_relationship",
            )
        ]

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.source_character_id and self.project_id:
            if self.source_character.project_id != self.project_id:
                errors["source_character"] = (
                    "Source character must belong to the relationship project."
                )
        if self.target_character_id and self.project_id:
            if self.target_character.project_id != self.project_id:
                errors["target_character"] = (
                    "Target character must belong to the relationship project."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)
