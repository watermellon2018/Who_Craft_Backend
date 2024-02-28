from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.contrib.auth.models import Group, Permission
from django.utils.translation import gettext_lazy as _


# class UserManager(BaseUserManager):
#     def create_user(self, password=None, **extra_fields):
#         user = self.model(
#             **extra_fields
#         )
#
#         user.set_password(password)
#         user.save(using=self._db)
#
#         return user
#
#     def create_superuser(self, password, **extra_fields):
#         extra_fields.setdefault('is_staff', True)
#         extra_fields.setdefault('is_superuser', True)
#
#         return self.create_user(password, **extra_fields)
#
# import uuid
# class User(AbstractBaseUser, PermissionsMixin):
#     username = models.CharField(max_length=30, blank=True)
#     is_staff = models.BooleanField(default=False)
#     date_joined = models.DateTimeField(auto_now_add=True)
#
#     objects = UserManager()
#
#     USERNAME_FIELD = 'username'
#
#     groups = models.ManyToManyField(
#         Group,
#         verbose_name=_('groups'),
#         blank=True,
#         help_text=_(
#             'The groups this user belongs to. A user will get all permissions '
#             'granted to each of their groups.'
#         ),
#         related_name='health_user_groups'
#     )
#     user_permissions = models.ManyToManyField(
#         Permission,
#         verbose_name=_('user permissions'),
#         blank=True,
#         help_text=_('Specific permissions for this user.'),
#         related_name='health_user_permissions'
#     )
#
#     def __str__(self):
#         return self.id

from django.contrib.auth.models import User
import uuid
class UserKey(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    key = models.UUIDField(default=uuid.uuid4, editable=False)

