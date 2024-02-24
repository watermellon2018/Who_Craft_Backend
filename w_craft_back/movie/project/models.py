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
