import uuid

from django.db import models
from mptt.models import MPTTModel, TreeForeignKey


class MenuFolder(MPTTModel):
    name = models.CharField(max_length=100)
    parent = TreeForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    is_folder = models.BooleanField(default=False)

    class MPTTMeta:
        order_insertion_by = ['name']
        app_label = "w_craft_back"

    def __str__(self):
        return self.name


class ItemFolder(MenuFolder):
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    # parent = TreeForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')

    class MPTTMeta:
        order_insertion_by = ['name']
        app_label = "w_craft_back"

    def __str__(self):
        return self.name


from rest_framework import serializers

# class ItemFolderSerializer(serializers.ModelSerializer):
#      class Meta:
#          model = ItemFolder
#          fields = "__all__"
