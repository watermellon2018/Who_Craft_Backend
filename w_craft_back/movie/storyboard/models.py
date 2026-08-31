"""Persistence models for scene storyboards and keyframe generations."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.project.dashboard_models import (
    Location,
    ProjectAsset,
    SceneStoryboard,
)
from w_craft_back.movie.reference_library.models import ProjectReference


class SceneStoryboardEditorDraft(models.Model):
    """Durable editable working copy, separate from rendered storyboard history."""

    scene = models.OneToOneField(
        "w_craft_back.Scene",
        on_delete=models.CASCADE,
        related_name="storyboard_editor_draft",
    )
    payload = models.JSONField()
    revision = models.PositiveIntegerField(default=1)
    last_mutation_id = models.UUIDField()
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)


class StoryboardKeyframeType(models.TextChoices):
    START = "start", "Start"
    INTERMEDIATE = "intermediate", "Intermediate"
    END = "end", "End"


class ShotReferenceRole(models.TextChoices):
    LOCATION = "location", "Location"
    OBJECT = "object", "Object"
    CLOTHING = "clothing", "Clothing"
    TRANSPORT = "transport", "Transport"
    OTHER = "other", "Other"


class CameraAzimuth(models.TextChoices):
    FRONT = "front", "Front"
    FRONT_LEFT = "front_left", "Front left"
    LEFT = "left", "Left"
    BACK_LEFT = "back_left", "Back left"
    BACK = "back", "Back"
    BACK_RIGHT = "back_right", "Back right"
    RIGHT = "right", "Right"
    FRONT_RIGHT = "front_right", "Front right"


class CameraElevation(models.TextChoices):
    LOW = "low", "Low"
    EYE_LEVEL = "eye_level", "Eye level"
    HIGH = "high", "High"
    TOP = "top", "Top"


class CameraDistance(models.TextChoices):
    WIDE = "wide", "Wide"
    MEDIUM = "medium", "Medium"
    NEAR = "near", "Near"


class CameraFraming(models.TextChoices):
    EXTREME_WIDE = "extreme_wide", "Extreme wide"
    WIDE = "wide", "Wide"
    FULL = "full", "Full"
    MEDIUM = "medium", "Medium"
    MEDIUM_CLOSE = "medium_close", "Medium close"
    CLOSE = "close", "Close"
    EXTREME_CLOSE = "extreme_close", "Extreme close"
    OTS = "ots", "Over the shoulder"
    POV = "pov", "Point of view"


class CameraMovement(models.TextChoices):
    STATIC = "static", "Static"
    DOLLY_IN = "dolly_in", "Dolly in"
    DOLLY_OUT = "dolly_out", "Dolly out"
    PAN_LEFT = "pan_left", "Pan left"
    PAN_RIGHT = "pan_right", "Pan right"
    TILT_UP = "tilt_up", "Tilt up"
    TILT_DOWN = "tilt_down", "Tilt down"
    ORBIT_LEFT = "orbit_left", "Orbit left"
    ORBIT_RIGHT = "orbit_right", "Orbit right"
    TRUCK_LEFT = "truck_left", "Truck left"
    TRUCK_RIGHT = "truck_right", "Truck right"
    CRANE_UP = "crane_up", "Crane up"
    CRANE_DOWN = "crane_down", "Crane down"
    FOLLOW = "follow", "Follow"
    CUSTOM = "custom", "Custom"


class GenerationReferenceType(models.TextChoices):
    CHARACTER = "character", "Character"
    LOCATION = "location", "Location"
    OBJECT = "object", "Object"
    CLOTHING = "clothing", "Clothing"
    PREVIOUS_KEYFRAME = "previous_keyframe", "Previous keyframe"
    PREVIOUS_SHOT = "previous_shot", "Previous shot"
    OTHER_STORYBOARD_KEYFRAME = (
        "other_storyboard_keyframe",
        "Other storyboard keyframe",
    )


class StoryboardGenerationStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    GENERATING = "generating", "Generating"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


def _shot_project_id(shot: StoryboardShot) -> Any:
    """Return the project id that owns a shot."""

    return shot.storyboard.scene.project_id


def _keyframe_project_id(keyframe: StoryboardKeyframe) -> Any:
    """Return the project id that owns a keyframe."""

    return _shot_project_id(keyframe.shot)


class StoryboardShot(models.Model):
    """An ordered camera shot in the storyboard for one scene."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    storyboard = models.ForeignKey(
        SceneStoryboard,
        on_delete=models.CASCADE,
        related_name="shots",
    )
    order = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    title = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    duration_seconds = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storyboard_shots",
    )
    characters = models.ManyToManyField(
        StudioCharacter,
        through="StoryboardShotCharacter",
        related_name="storyboard_shots",
        blank=True,
    )
    version = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_storyboard_shots",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_storyboard_shots",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "created_at"]
        indexes = [
            models.Index(fields=["storyboard", "order"]),
            models.Index(fields=["location"]),
            models.Index(fields=["storyboard", "updated_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(order__gte=1),
                name="chk_storyboard_shot_order",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(duration_seconds__isnull=True)
                    | models.Q(duration_seconds__gte=0)
                ),
                name="chk_storyboard_shot_duration",
            ),
            models.UniqueConstraint(
                fields=["storyboard", "order"],
                name="uniq_storyboard_shot_order",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.location_id and self.storyboard_id:
            if self.location.project_id != self.storyboard.scene.project_id:
                raise ValidationError(
                    {"location": "Location must belong to the scene project."}
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"Shot {self.order}: {self.title}"


class StoryboardShotCharacter(models.Model):
    """A project-safe character link that survives character deletion."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shot = models.ForeignKey(
        StoryboardShot,
        on_delete=models.CASCADE,
        related_name="character_links",
    )
    character = models.ForeignKey(
        StudioCharacter,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storyboard_shot_links",
    )
    name_snapshot = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["shot", "character"])]
        constraints = [
            models.UniqueConstraint(
                fields=["shot", "character"],
                condition=models.Q(character__isnull=False),
                name="uniq_storyboard_shot_character",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.shot_id and self.character_id:
            if _shot_project_id(self.shot) != self.character.project_id:
                raise ValidationError(
                    {"character": "Character must belong to the scene project."}
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        return super().save(*args, **kwargs)


class StoryboardShotReference(models.Model):
    """A role-qualified Visual Library reference attached to a shot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shot = models.ForeignKey(
        StoryboardShot,
        on_delete=models.CASCADE,
        related_name="visual_references",
    )
    reference = models.ForeignKey(
        ProjectReference,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storyboard_shot_links",
    )
    role = models.CharField(max_length=16, choices=ShotReferenceRole.choices)
    title_snapshot = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["shot", "role"]),
            models.Index(fields=["reference"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["shot", "reference", "role"],
                condition=models.Q(reference__isnull=False),
                name="uniq_storyboard_shot_reference",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.shot_id and self.reference_id:
            if _shot_project_id(self.shot) != self.reference.project_id:
                raise ValidationError(
                    {"reference": "Reference must belong to the scene project."}
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        return super().save(*args, **kwargs)


class StoryboardKeyframe(models.Model):
    """A normalized point on a shot timeline."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shot = models.ForeignKey(
        StoryboardShot,
        on_delete=models.CASCADE,
        related_name="keyframes",
    )
    type = models.CharField(max_length=16, choices=StoryboardKeyframeType.choices)
    position = models.DecimalField(max_digits=5, decimal_places=4)
    current_generation = models.ForeignKey(
        "StoryboardKeyframeGeneration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "created_at"]
        indexes = [
            models.Index(fields=["shot", "position"]),
            models.Index(fields=["shot", "type"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        type=StoryboardKeyframeType.START,
                        position=Decimal("0"),
                    )
                    | models.Q(
                        type=StoryboardKeyframeType.END,
                        position=Decimal("1"),
                    )
                    | (
                        models.Q(
                            position__gt=0,
                            position__lt=1,
                            type=StoryboardKeyframeType.INTERMEDIATE,
                        )
                    )
                ),
                name="chk_storyboard_keyframe_type_position",
            ),
            models.UniqueConstraint(
                fields=["shot", "position"],
                name="uniq_storyboard_keyframe_position",
            ),
            models.UniqueConstraint(
                fields=["shot", "type"],
                condition=models.Q(
                    type__in=(
                        StoryboardKeyframeType.START,
                        StoryboardKeyframeType.END,
                    )
                ),
                name="uniq_storyboard_keyframe_boundary",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.position is not None:
            if self.position < Decimal("0") or self.position > Decimal("1"):
                errors["position"] = "Position must be between 0 and 1."
            elif (
                self.type == StoryboardKeyframeType.START
                and self.position != Decimal("0")
            ):
                errors["position"] = "Start keyframe position must be 0."
            elif (
                self.type == StoryboardKeyframeType.END
                and self.position != Decimal("1")
            ):
                errors["position"] = "End keyframe position must be 1."
            elif (
                self.type == StoryboardKeyframeType.INTERMEDIATE
                and not Decimal("0") < self.position < Decimal("1")
            ):
                errors["position"] = (
                    "Intermediate keyframe position must be between 0 and 1."
                )
        if (
            self.current_generation_id
            and self.current_generation.keyframe_id != self.id
        ):
            errors["current_generation"] = (
                "Current generation must belong to this keyframe."
            )
        if errors:
            raise ValidationError(errors)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        return super().save(*args, **kwargs)


def _normalized_decimal(value: Any, field_name: str) -> Decimal:
    """Convert a JSON number to Decimal or raise a field validation error."""

    if isinstance(value, bool):
        raise ValidationError({"composition": f"{field_name} must be numeric."})
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(
            {"composition": f"{field_name} must be numeric."}
        ) from exc


def _validate_composition(composition: Any) -> None:
    """Validate normalized two-dimensional subject bounds."""

    if not isinstance(composition, list):
        raise ValidationError({"composition": "Composition must be a list."})
    for index, subject in enumerate(composition):
        if not isinstance(subject, dict):
            raise ValidationError(
                {"composition": f"Composition item {index} must be an object."}
            )
        values = {
            name: _normalized_decimal(subject.get(name), name)
            for name in ("x", "y", "width", "height")
        }
        if not Decimal("0") <= values["x"] <= Decimal("1"):
            raise ValidationError({"composition": f"Item {index} x is invalid."})
        if not Decimal("0") <= values["y"] <= Decimal("1"):
            raise ValidationError({"composition": f"Item {index} y is invalid."})
        if not Decimal("0") < values["width"] <= Decimal("1"):
            raise ValidationError(
                {"composition": f"Item {index} width is invalid."}
            )
        if not Decimal("0") < values["height"] <= Decimal("1"):
            raise ValidationError(
                {"composition": f"Item {index} height is invalid."}
            )
        if values["x"] + values["width"] > Decimal("1"):
            raise ValidationError(
                {"composition": f"Item {index} exceeds the frame width."}
            )
        if values["y"] + values["height"] > Decimal("1"):
            raise ValidationError(
                {"composition": f"Item {index} exceeds the frame height."}
            )


class CameraIntent(models.Model):
    """Structured provider-independent camera direction for a keyframe."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyframe = models.OneToOneField(
        StoryboardKeyframe,
        on_delete=models.CASCADE,
        related_name="camera_intent",
    )
    target = models.JSONField(default=dict, blank=True)
    azimuth = models.CharField(
        max_length=16,
        choices=CameraAzimuth.choices,
        default=CameraAzimuth.FRONT,
    )
    elevation = models.CharField(
        max_length=16,
        choices=CameraElevation.choices,
        default=CameraElevation.EYE_LEVEL,
    )
    distance = models.CharField(
        max_length=16,
        choices=CameraDistance.choices,
        default=CameraDistance.MEDIUM,
    )
    framing = models.CharField(
        max_length=20,
        choices=CameraFraming.choices,
        default=CameraFraming.MEDIUM,
    )
    lens_mm = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(10), MaxValueValidator(300)],
    )
    composition = models.JSONField(default=list, blank=True)
    camera_metadata = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["updated_at"])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(lens_mm__isnull=True)
                    | (models.Q(lens_mm__gte=10) & models.Q(lens_mm__lte=300))
                ),
                name="chk_camera_intent_lens",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if not isinstance(self.target, dict):
            raise ValidationError({"target": "Camera target must be an object."})
        if not isinstance(self.camera_metadata, dict):
            raise ValidationError(
                {"camera_metadata": "Camera metadata must be an object."}
            )
        _validate_composition(self.composition)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        return super().save(*args, **kwargs)


class CameraTransition(models.Model):
    """Detected camera movement and an optional user override for one edge."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shot = models.ForeignKey(
        StoryboardShot,
        on_delete=models.CASCADE,
        related_name="transitions",
    )
    from_keyframe = models.ForeignKey(
        StoryboardKeyframe,
        on_delete=models.CASCADE,
        related_name="outgoing_transitions",
    )
    to_keyframe = models.ForeignKey(
        StoryboardKeyframe,
        on_delete=models.CASCADE,
        related_name="incoming_transitions",
    )
    detected_movement = models.CharField(
        max_length=16,
        choices=CameraMovement.choices,
        default=CameraMovement.STATIC,
    )
    override_movement = models.CharField(
        max_length=16,
        choices=CameraMovement.choices,
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["from_keyframe__position", "to_keyframe__position"]
        indexes = [
            models.Index(fields=["shot", "from_keyframe"]),
            models.Index(fields=["shot", "to_keyframe"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["shot", "from_keyframe", "to_keyframe"],
                name="uniq_storyboard_camera_transition",
            ),
            models.CheckConstraint(
                condition=~models.Q(from_keyframe=models.F("to_keyframe")),
                name="chk_storyboard_transition_distinct",
            ),
        ]

    @property
    def effective_movement(self) -> str:
        return self.override_movement or self.detected_movement

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if self.from_keyframe_id and self.from_keyframe.shot_id != self.shot_id:
            errors["from_keyframe"] = "From keyframe must belong to the shot."
        if self.to_keyframe_id and self.to_keyframe.shot_id != self.shot_id:
            errors["to_keyframe"] = "To keyframe must belong to the shot."
        if self.from_keyframe_id == self.to_keyframe_id:
            errors["to_keyframe"] = "Transition keyframes must be different."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        return super().save(*args, **kwargs)


class KeyframeGenerationReference(models.Model):
    """A user-selected continuity or project reference for generation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyframe = models.ForeignKey(
        StoryboardKeyframe,
        on_delete=models.CASCADE,
        related_name="generation_references",
    )
    reference_type = models.CharField(
        max_length=32,
        choices=GenerationReferenceType.choices,
    )
    source_keyframe = models.ForeignKey(
        StoryboardKeyframe,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="used_as_generation_reference",
    )
    visual_reference = models.ForeignKey(
        ProjectReference,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storyboard_generation_references",
    )
    character = models.ForeignKey(
        StudioCharacter,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storyboard_generation_references",
    )
    location = models.ForeignKey(
        Location,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storyboard_generation_references",
    )
    priority = models.PositiveSmallIntegerField(default=0)
    is_primary = models.BooleanField(default=False)
    label_snapshot = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "created_at"]
        indexes = [
            models.Index(fields=["keyframe", "priority"]),
            models.Index(fields=["source_keyframe"]),
            models.Index(fields=["visual_reference"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["keyframe"],
                condition=models.Q(is_primary=True),
                name="uniq_storyboard_primary_reference",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if not self.keyframe_id:
            return
        project_id = _keyframe_project_id(self.keyframe)
        errors: dict[str, str] = {}
        if self.source_keyframe_id:
            if _keyframe_project_id(self.source_keyframe) != project_id:
                errors["source_keyframe"] = (
                    "Source keyframe must belong to the scene project."
                )
        if self.visual_reference_id:
            if self.visual_reference.project_id != project_id:
                errors["visual_reference"] = (
                    "Reference must belong to the scene project."
                )
        if self.character_id and self.character.project_id != project_id:
            errors["character"] = "Character must belong to the scene project."
        if self.location_id and self.location.project_id != project_id:
            errors["location"] = "Location must belong to the scene project."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        return super().save(*args, **kwargs)


class StoryboardKeyframeGeneration(models.Model):
    """Immutable request provenance and durable execution state for one image."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    keyframe = models.ForeignKey(
        StoryboardKeyframe,
        on_delete=models.CASCADE,
        related_name="generations",
    )
    revision = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    asset = models.ForeignKey(
        ProjectAsset,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="storyboard_keyframe_generations",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="storyboard_keyframe_generations",
    )
    request_snapshot = models.JSONField(default=dict)
    request_fingerprint = models.CharField(max_length=64)
    requested_model = models.CharField(max_length=128, blank=True, default="")
    provider_snapshot = models.JSONField(default=dict, blank=True)
    provider = models.CharField(max_length=64, blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="")
    selected_provider = models.CharField(max_length=64, blank=True, default="")
    selected_model = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=StoryboardGenerationStatus.choices,
        default=StoryboardGenerationStatus.QUEUED,
    )
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1)],
    )
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    provider_started_at = models.DateTimeField(null=True, blank=True)
    provider_result_received_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=128, blank=True, default="")
    error_detail = models.CharField(max_length=500, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-revision", "-created_at"]
        indexes = [
            models.Index(fields=["keyframe", "-revision"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["status", "lease_expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["keyframe", "revision"],
                name="uniq_storyboard_generation_revision",
            ),
            models.UniqueConstraint(
                fields=["keyframe"],
                condition=models.Q(
                    status__in=(
                        StoryboardGenerationStatus.QUEUED,
                        StoryboardGenerationStatus.GENERATING,
                    )
                ),
                name="uniq_storyboard_active_generation",
            ),
            models.UniqueConstraint(
                fields=["keyframe", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_storyboard_generation_idempotency",
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__lte=models.F("max_attempts")),
                name="chk_storyboard_generation_attempts",
            ),
        ]

    def _validate_immutable_request(self) -> None:
        if self._state.adding or not self.pk:
            return
        original = type(self).objects.filter(pk=self.pk).values(
            "request_snapshot",
            "request_fingerprint",
            "requested_model",
            "provider_snapshot",
            "provider",
            "model",
        ).first()
        if original is None:
            return
        current = {
            "request_snapshot": self.request_snapshot,
            "request_fingerprint": self.request_fingerprint,
            "requested_model": self.requested_model,
            "provider_snapshot": self.provider_snapshot,
            "provider": self.provider,
            "model": self.model,
        }
        if current != original:
            raise ValidationError(
                "Generation request and provider route are immutable."
            )

    def clean(self) -> None:
        super().clean()
        if self.asset_id and self.keyframe_id:
            if self.asset.project_id != _keyframe_project_id(self.keyframe):
                raise ValidationError(
                    {"asset": "Generation asset must belong to the scene project."}
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self._validate_immutable_request()
        self.clean()
        return super().save(*args, **kwargs)
