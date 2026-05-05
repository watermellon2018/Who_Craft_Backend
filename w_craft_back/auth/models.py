import uuid

from django.contrib.auth.models import User
from django.db import models


class UserKey(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    key = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

