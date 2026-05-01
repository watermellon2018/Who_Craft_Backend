import uuid

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
    EXPRESSION = "expression", "Expression"
    OUTFIT_REFERENCE = "outfit_reference", "Outfit reference"
    THUMBNAIL = "thumbnail", "Thumbnail"


class GenerationJobType(models.TextChoices):
    INITIAL_VARIANTS = "initial_variants", "Initial variants"
    EDIT_VARIANTS = "edit_variants", "Edit variants"
    OUTFIT_VARIANTS = "outfit_variants", "Outfit variants"
    EXPRESSION_VARIANTS = "expression_variants", "Expression variants"
    CHARACTER_SHEET = "character_sheet", "Character sheet"
    REFERENCE_EXTRACTION = "reference_extraction", "Reference extraction"


class GenerationJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
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
    user = models.ForeignKey(UserKey, on_delete=models.CASCADE, related_name="studio_characters")
    name = models.CharField(max_length=255)
    character_type = models.CharField(max_length=32, choices=CharacterType.choices, default=CharacterType.HUMAN)
    role = models.CharField(max_length=100, blank=True, default="")
    short_description = models.TextField(blank=True, default="")
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    lifecycle_stage = models.CharField(max_length=128, blank=True, default="")
    gender = models.CharField(max_length=100, blank=True, default="")
    species = models.CharField(max_length=100, default="human")
    visual_style = models.CharField(max_length=100, blank=True, default="")
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
            )
        ]

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


class CharacterAsset(models.Model):
    asset_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="assets")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="character_assets")
    user = models.ForeignKey(UserKey, on_delete=models.CASCADE, related_name="character_assets")
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
    model_name = models.CharField(max_length=100, blank=True, default="")
    model_version = models.CharField(max_length=100, blank=True, default="")
    seed = models.BigIntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    safety_status = models.CharField(max_length=100, blank=True, default="unchecked")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "character_assets"
        indexes = [
            models.Index(fields=["character"], name="character_a_charact_0d1067_idx"),
            models.Index(fields=["project"], name="character_a_project_d415f6_idx"),
            models.Index(fields=["asset_type"], name="character_a_asset_t_07aa97_idx"),
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


class CharacterGenerationJob(models.Model):
    job_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="generation_jobs")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="character_generation_jobs")
    user = models.ForeignKey(UserKey, on_delete=models.CASCADE, related_name="character_generation_jobs")
    job_type = models.CharField(max_length=64, choices=GenerationJobType.choices)
    status = models.CharField(max_length=32, choices=GenerationJobStatus.choices, default=GenerationJobStatus.QUEUED)
    region = models.CharField(max_length=32, choices=CharacterRegion.choices, default=CharacterRegion.FULL_CHARACTER)
    variant_count = models.PositiveSmallIntegerField(default=4)
    request_payload = models.JSONField(default=dict, blank=True)
    compiled_prompt = models.TextField(blank=True, default="")
    negative_prompt = models.TextField(blank=True, default="")
    edit_instruction = models.TextField(blank=True, default="")
    preserve_options = models.JSONField(default=dict, blank=True)
    provider = models.CharField(max_length=100, blank=True, default="mock")
    model_name = models.CharField(max_length=100, blank=True, default="")
    model_version = models.CharField(max_length=100, blank=True, default="")
    progress = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    error_code = models.CharField(max_length=100, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "character_generation_jobs"
        indexes = [
            models.Index(fields=["character"], name="character_g_charact_18486e_idx"),
            models.Index(fields=["project"], name="character_g_project_a65b63_idx"),
            models.Index(fields=["status"], name="character_g_status_a1b6e0_idx"),
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
        ]


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


class CharacterRevision(models.Model):
    revision_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(StudioCharacter, on_delete=models.CASCADE, related_name="revisions")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="character_revisions")
    user = models.ForeignKey(UserKey, on_delete=models.CASCADE, related_name="character_revisions")
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
