import uuid

from django.db import models
from mptt.models import MPTTModel, TreeForeignKey

from w_craft_back.auth.models import UserKey
from w_craft_back.characters.creating.models import Character
from w_craft_back.movie.project.models import Project


class MenuFolder(MPTTModel):
    key = models.UUIDField(default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    parent = TreeForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    is_folder = models.BooleanField(default=False)

    user = models.ForeignKey(UserKey, null=True, on_delete=models.CASCADE)
    cur_project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True)

    class MPTTMeta:
        order_insertion_by = ['name']
        app_label = "w_craft_back"

    def __str__(self):
        return self.name


class ItemFolder(MenuFolder):
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    hero = models.ForeignKey(Character, on_delete=models.CASCADE, null=True)

    class MPTTMeta:
        order_insertion_by = ['name']
        app_label = "w_craft_back"

    def __str__(self):
        return self.name
