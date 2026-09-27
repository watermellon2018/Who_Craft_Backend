"""Only ciphertext and a display hint persist for personal provider secrets."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


PROVIDERS = (
    ('gemini', 'Google Gemini'),
    ('openrouter', 'OpenRouter'),
    ('stability', 'Stability AI'),
    ('elevenlabs', 'ElevenLabs'),
    ('minimax', 'MiniMax'),
)
MODALITIES = ('text', 'image', 'music', 'sound_effect', 'video')
SOURCES = (('platform', 'API Craft'), ('personal', 'My API'))


class PersonalAPIConnection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='personal_api_connections',
    )
    name = models.CharField(max_length=100)
    provider = models.CharField(max_length=32, choices=PROVIDERS)
    encrypted_key = models.TextField(blank=True, editable=False)
    key_hint = models.CharField(max_length=12, blank=True, editable=False)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ('created_at', 'id')
        indexes = [models.Index(
            fields=('user', 'is_active'), name='personal_api_owner_active',
        )]

    def __str__(self) -> str:
        # Avoid user-supplied names or credential material in repr/debug output.
        return f'PersonalAPIConnection({self.pk})'


class PersonalAPIPreference(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='personal_api_preferences',
    )
    modality = models.CharField(
        max_length=20, choices=tuple((value, value) for value in MODALITIES),
    )
    source = models.CharField(max_length=12, choices=SOURCES, default='platform')
    routes = models.JSONField(default=list)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=('user', 'modality'), name='personal_api_user_modality_unique',
        )]
