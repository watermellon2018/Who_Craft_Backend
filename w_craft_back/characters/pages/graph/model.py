from django.db import models
from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.models import Project

class RelationshipType(models.Model):
    name = models.CharField(max_length=255)
    translit = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class GraphEdge(models.Model):
    user = models.ForeignKey(UserKey, on_delete=models.CASCADE)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    from_node = models.CharField(max_length=255)  # Имя "откуда" узла
    to_node = models.CharField(max_length=255)  # Имя "куда" узла
    label = models.ForeignKey(RelationshipType, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.project.title}: {self.from_node} -> {self.to_node} ({self.label})"