from django.db import models


class Genre(models.Model):
    name = models.CharField(max_length=100)
    translit = models.CharField(max_length=100, default='')

    def __str__(self):
        return self.name


class Audience(models.Model):
    name = models.CharField(max_length=255)
    translit = models.CharField(max_length=100, default='')

    def __str__(self):
        return self.name
