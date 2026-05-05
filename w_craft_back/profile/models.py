from django.contrib.auth.models import User
from django.db import models


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    display_name = models.CharField(max_length=255, blank=True, default='')
    tagline = models.CharField(max_length=255, blank=True, default='')
    bio = models.TextField(blank=True, default='')
    location = models.CharField(max_length=255, blank=True, default='')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    cover = models.ImageField(upload_to='covers/', blank=True, null=True)
    language = models.CharField(max_length=10, default='ru')
    private_account = models.BooleanField(default=False)
    notifications_enabled = models.BooleanField(default=True)
    favorite_genres = models.JSONField(default=list, blank=True)
    interests = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_profiles'

    def get_profile_completion(self):
        items = {
            'avatar': bool(self.avatar),
            'about': bool(self.bio),
            'interests': bool(self.interests),
            'socials': False,
        }
        filled = sum(1 for v in items.values() if v)
        percent = round(filled / len(items) * 100)
        return {'percent': percent, 'items': items}
