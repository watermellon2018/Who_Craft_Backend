import re

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify


USERNAME_RE = re.compile(r'^[a-z0-9_]{3,30}$')


def validate_public_username(value: str) -> None:
    if not USERNAME_RE.match(value):
        raise ValidationError(
            'username must be 3-30 chars, lowercase latin letters, digits or "_"'
        )


class UserAsset(models.Model):
    AVATAR = 'avatar'
    COVER = 'cover'
    OTHER = 'other'
    TYPE_CHOICES = [
        (AVATAR, 'avatar'),
        (COVER, 'cover'),
        (OTHER, 'other'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='assets')
    type = models.CharField(max_length=16, choices=TYPE_CHOICES, default=OTHER)
    storage_key = models.CharField(max_length=512)
    url = models.URLField(max_length=2048, blank=True, null=True)
    mime_type = models.CharField(max_length=128, blank=True, null=True)
    size_bytes = models.BigIntegerField(blank=True, null=True)
    width = models.IntegerField(blank=True, null=True)
    height = models.IntegerField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'user_assets'
        indexes = [
            models.Index(fields=['user'], name='user_assets_user_idx'),
            models.Index(fields=['user', 'type'], name='user_assets_user_type_idx'),
        ]


class Interest(models.Model):
    name = models.CharField(max_length=64, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'interests'
        ordering = ['name']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True) or self.name.lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class UserInterest(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='user_interests')
    interest = models.ForeignKey(Interest, on_delete=models.CASCADE, related_name='user_interests')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_interests'
        constraints = [
            models.UniqueConstraint(fields=['user', 'interest'], name='user_interest_unique'),
        ]
        indexes = [
            models.Index(fields=['user'], name='user_interests_user_idx'),
            models.Index(fields=['interest'], name='user_interests_interest_idx'),
        ]


class UserSocialLink(models.Model):
    TELEGRAM = 'telegram'
    INSTAGRAM = 'instagram'
    YOUTUBE = 'youtube'
    WEBSITE = 'website'
    TIKTOK = 'tiktok'
    X = 'x'
    VK = 'vk'
    OTHER = 'other'
    PLATFORM_CHOICES = [
        (TELEGRAM, 'Telegram'),
        (INSTAGRAM, 'Instagram'),
        (YOUTUBE, 'YouTube'),
        (WEBSITE, 'Website'),
        (TIKTOK, 'TikTok'),
        (X, 'X'),
        (VK, 'VK'),
        (OTHER, 'Other'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='social_links')
    platform = models.CharField(max_length=32, choices=PLATFORM_CHOICES)
    url = models.URLField(max_length=2048)
    display_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_social_links'
        constraints = [
            models.UniqueConstraint(fields=['user', 'platform'], name='user_social_link_unique'),
        ]
        indexes = [
            models.Index(fields=['user'], name='user_social_links_user_idx'),
            models.Index(fields=['user', 'display_order'], name='user_social_links_order_idx'),
        ]


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    public_username = models.CharField(
        max_length=30,
        unique=True,
        blank=True,
        null=True,
        validators=[validate_public_username],
    )
    display_name = models.CharField(max_length=255, blank=True, default='')
    tagline = models.CharField(max_length=255, blank=True, default='')
    bio = models.TextField(blank=True, default='')
    location = models.CharField(max_length=255, blank=True, default='')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    cover = models.ImageField(upload_to='covers/', blank=True, null=True)
    avatar_asset = models.ForeignKey(
        UserAsset,
        on_delete=models.SET_NULL,
        related_name='+',
        blank=True,
        null=True,
    )
    cover_asset = models.ForeignKey(
        UserAsset,
        on_delete=models.SET_NULL,
        related_name='+',
        blank=True,
        null=True,
    )
    language = models.CharField(max_length=10, default='ru')
    private_account = models.BooleanField(default=False)
    notifications_enabled = models.BooleanField(default=True)
    favorite_genres = models.JSONField(default=list, blank=True)
    interests = models.JSONField(default=list, blank=True)
    subscribers_count = models.IntegerField(default=0)
    subscriptions_count = models.IntegerField(default=0)
    image_generation_model = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Image model registry or catalog key, e.g. "gemini-imagen-4". '
                  'Empty falls back to env/registry default.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_profiles'

    @property
    def effective_username(self) -> str:
        return self.public_username or self.user.username

    def get_profile_completion(self):
        items = {
            'avatar': bool(self.avatar) or self.avatar_asset_id is not None,
            'about': len((self.bio or '').strip()) >= 10,
            'interests': bool(self.interests) or self.user.user_interests.exists(),
            'socials': self.user.social_links.exists(),
        }
        filled = sum(1 for v in items.values() if v)
        percent = round(filled / len(items) * 100)
        return {'percent': percent, 'items': items}
