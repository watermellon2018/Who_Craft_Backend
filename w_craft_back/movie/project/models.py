from w_craft_back.movie.properties.models import Genre, Audience

from django.db import models


class Project(models.Model):
    title = models.CharField(max_length=255)
    image = models.ImageField(upload_to='project/poster/')  # Поле для загрузки изображения
    genre = models.ManyToManyField(Genre)
    format = models.CharField(max_length=255)
    audience = models.ManyToManyField(Audience)
    annot = models.TextField()
    desc = models.TextField()

    def __str__(self):
        return self.title

# import base64
# from rest_framework import serializers
#
# class ProjectSerializer(serializers.ModelSerializer):
#     image_data = serializers.SerializerMethodField()
#
#     class Meta:
#         model = Project
#         fields = ['id', 'title', 'image', 'format', 'description', 'annot']
#
#     def get_image_data(self, obj):
#         if obj.image:
#             try:
#                 with open(obj.image.path, "rb") as img_file:
#                     return base64.b64encode(img_file.read()).decode('utf-8')
#             except FileNotFoundError:
#                 return None
#         return None