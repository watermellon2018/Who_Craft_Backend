from rest_framework import serializers

from w_craft_back.storage_gateway import signed_url_for_file

from .models import UserProfile, UserSocialLink, USERNAME_RE


class UserProfileSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = [
            'language',
            'content_language',
            'private_account',
            'notifications_in_app',
            'notifications_email',
            'comment_permission',
        ]


class SocialLinkSerializer(serializers.Serializer):
    platform = serializers.ChoiceField(
        choices=[choice[0] for choice in UserSocialLink.PLATFORM_CHOICES]
    )
    url = serializers.URLField(max_length=2048)
    display_order = serializers.IntegerField(required=False, default=0)


class ProfileMeUpdateSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    public_username = serializers.CharField(
        max_length=32, required=False, allow_null=True, allow_blank=True
    )
    bio = serializers.CharField(max_length=100, required=False, allow_blank=True)
    tagline = serializers.CharField(max_length=255, required=False, allow_blank=True)
    location = serializers.CharField(max_length=255, required=False, allow_blank=True)
    language = serializers.ChoiceField(choices=['ru', 'en'], required=False)
    content_language = serializers.ChoiceField(
        choices=UserProfile.ContentLanguage.values,
        required=False,
    )
    private_account = serializers.BooleanField(required=False)
    notifications_in_app = serializers.BooleanField(required=False)
    notifications_email = serializers.BooleanField(required=False)
    comment_permission = serializers.ChoiceField(
        choices=UserProfile.CommentPermission.values,
        required=False,
    )
    interests = serializers.ListField(
        child=serializers.CharField(max_length=32),
        required=False,
        max_length=10,
    )

    def validate_interests(self, value):
        if len(value) > 10:
            raise serializers.ValidationError(
                'PROFILE_INTERESTS_LIMIT_EXCEEDED',
                code='interests_limit_exceeded',
            )
        return value

    socials = serializers.ListField(
        child=SocialLinkSerializer(),
        required=False,
        max_length=20,
    )

    def validate_public_username(self, value):
        if value in (None, ''):
            return None
        if not USERNAME_RE.match(value):
            raise serializers.ValidationError(
                'username must be 3-32 chars, lowercase latin letters, digits, "_" or "-"'
            )
        user = self.context['user']
        clash = (
            UserProfile.objects
            .filter(public_username=value)
            .exclude(user=user)
            .exists()
        )
        if clash:
            raise serializers.ValidationError('username already taken', code='conflict')
        return value


def _abs_media_url(request, image_field):
    return signed_url_for_file(image_field, request)


def serialize_profile_me(profile: UserProfile, request) -> dict:
    user = profile.user
    completion = profile.get_profile_completion()

    interests = list(
        profile.user.user_interests
        .select_related('interest')
        .order_by('interest__name')
        .values_list('interest__name', flat=True)
    )
    socials = [
        {'platform': s.platform, 'url': s.url, 'display_order': s.display_order}
        for s in profile.user.social_links.order_by('display_order', 'id')
    ]

    return {
        'user': {
            'id': user.id,
            'username': user.username,
            'public_username': profile.public_username,
            'effective_username': profile.effective_username,
            'display_name': profile.display_name or user.username,
            'tagline': profile.tagline,
            'bio': profile.bio,
            'location': profile.location,
            'avatar_url': _abs_media_url(request, profile.avatar),
            'cover_url': _abs_media_url(request, profile.cover),
            'joined_at': user.date_joined.strftime('%Y-%m-%d')
            if getattr(user, 'date_joined', None) else None,
        },
        'interests': interests,
        'socials': socials,
        'settings': {
            'language': profile.language,
            'content_language': profile.content_language,
            'private_account': profile.private_account,
            'notifications_in_app': profile.notifications_in_app,
            'notifications_email': profile.notifications_email,
            'comment_permission': profile.comment_permission,
            'image_generation_model': profile.image_generation_model or None,
        },
        'profile_completion': completion,
    }
