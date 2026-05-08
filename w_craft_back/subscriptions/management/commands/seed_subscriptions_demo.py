"""Dev seed for the Subscriptions page.

Creates demo users matching the current "Подписки" design and subscribes
the first existing user (or a freshly created `demo` user) to a curated set
of authors. Idempotent — re-running keeps state consistent.

Usage:
    python manage.py seed_subscriptions_demo
    python manage.py seed_subscriptions_demo --owner=<existing_username>
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from w_craft_back.profile.models import UserProfile
from w_craft_back.subscriptions import services


CHANNELS = [
    {'public_username': 'visualalchemist', 'display_name': 'Visual Alchemist', 'subscribers_count': 128000, 'favorite': True},
    {'public_username': 'cinematicai',     'display_name': 'CinematicAI',      'subscribers_count': 234000, 'favorite': True},
    {'public_username': 'neoframe',        'display_name': 'NeoFrame Studio',  'subscribers_count': 88000,  'favorite': False},
    {'public_username': 'dreamforge',      'display_name': 'DreamForge',       'subscribers_count': 75000,  'favorite': False},
    {'public_username': 'motionlab',       'display_name': 'MotionLab',        'subscribers_count': 64000,  'favorite': False},
    {'public_username': 'storypixel',      'display_name': 'StoryPixel',       'subscribers_count': 52000,  'favorite': False},
    {'public_username': 'framesmith',      'display_name': 'FrameSmith',       'subscribers_count': 29000,  'favorite': False},
    {'public_username': 'frameforge',      'display_name': 'FrameForge',       'subscribers_count': 75000,  'favorite': False},
    {'public_username': 'framelab',        'display_name': 'FrameLab',         'subscribers_count': 64000,  'favorite': False},
    {'public_username': 'framepioneer',    'display_name': 'FramePioneer',     'subscribers_count': 31000,  'favorite': False},
    {'public_username': 'echoframe',       'display_name': 'EchoFrame',        'subscribers_count': 18000,  'favorite': False},
    {'public_username': 'framecraft',      'display_name': 'FrameCraft',       'subscribers_count': 12000,  'favorite': False},
]


class Command(BaseCommand):
    help = 'Seed demo channels and subscribe the demo owner user to them.'

    def add_arguments(self, parser):
        parser.add_argument('--owner', default=None, help='username of the user who will be subscribed')

    @transaction.atomic
    def handle(self, *args, **opts):
        owner = self._resolve_owner(opts.get('owner'))
        self.stdout.write(self.style.SUCCESS(f'Owner: {owner.username} (id={owner.id})'))

        for ch in CHANNELS:
            user, created = User.objects.get_or_create(
                username=ch['public_username'],
                defaults={'is_active': True},
            )
            if created:
                user.set_unusable_password()
                user.save(update_fields=['password'])

            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.public_username = ch['public_username']
            profile.display_name = ch['display_name']
            profile.subscribers_count = ch['subscribers_count']
            profile.save(update_fields=['public_username', 'display_name', 'subscribers_count', 'updated_at'])

            if user.id == owner.id:
                continue

            services.subscribe(owner, user.id)
            if ch['favorite']:
                services.update_settings(owner, user.id, is_favorite=True)

        self.stdout.write(self.style.SUCCESS('Subscriptions seed complete.'))

    def _resolve_owner(self, owner_username: str | None) -> User:
        if owner_username:
            return User.objects.get(username=owner_username)
        # Fallback: the first non-staff user, or create `demo`.
        owner = User.objects.filter(is_active=True, is_staff=False).order_by('id').first()
        if owner is not None:
            return owner
        owner = User.objects.create(username='demo', is_active=True)
        owner.set_unusable_password()
        owner.save(update_fields=['password'])
        return owner
