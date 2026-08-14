import uuid

from django.db import models
from mptt.models import MPTTModel, TreeForeignKey

from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.project.models import Project


class MenuFolder(MPTTModel):
    """A project-scoped node in the Character Studio placement tree."""

    key = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    name = models.CharField(max_length=100)
    parent = TreeForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    is_folder = models.BooleanField(default=False)
    cur_project = models.ForeignKey(Project, on_delete=models.CASCADE)

    class MPTTMeta:
        order_insertion_by = ["name"]

    class Meta:
        app_label = "w_craft_back"

    def __str__(self) -> str:
        return self.name


class ItemFolder(MenuFolder):
    """A character placement, optionally linked during creation."""

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    studio_character = models.OneToOneField(
        StudioCharacter,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tree_placement",
    )

    class MPTTMeta:
        order_insertion_by = ["name"]

    class Meta:
        app_label = "w_craft_back"

    def __str__(self) -> str:
        return self.name
