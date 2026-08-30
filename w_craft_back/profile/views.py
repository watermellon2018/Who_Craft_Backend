from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from w_craft_back.api_errors import api_error_response
from w_craft_back.storage_gateway import signed_url_for_file

from .models import UserAsset, UserProfile
from .serializers import (
    ProfileMeUpdateSerializer,
    UserProfileSettingsSerializer,
    serialize_profile_me,
)
from .services import (
    AccountHasOwnedProjects,
    FileTooLarge,
    InactiveAccount,
    InvalidCurrentPassword,
    UnsupportedMediaType,
    close_user_account,
    delete_image,
    lock_active_user,
    replace_user_interests,
    replace_user_socials,
    save_uploaded_image,
)


class AccountDeleteThrottle(UserRateThrottle):
    """Separate per-user budget for destructive account closure attempts."""

    scope = 'profile_account_delete'
    rate = '5/hour'

    def get_cache_key(self, request, view):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        return super().get_cache_key(request, view)


def _get_user_from_request(request):
    """Return the Django user established by DRF authentication."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user
    return None


def _get_or_create_profile(user: User) -> UserProfile:
    with transaction.atomic():
        locked_user = lock_active_user(user)
        profile, _ = UserProfile.objects.select_for_update().get_or_create(
            user=locked_user,
        )
        return profile


def _inactive_account_response():
    return api_error_response(
        code='ACCOUNT_INACTIVE',
        message='account is inactive',
        status=status.HTTP_401_UNAUTHORIZED,
    )


class DashboardView(APIView):
    def get(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            profile = _get_or_create_profile(user)
        except InactiveAccount:
            return _inactive_account_response()
        completion = profile.get_profile_completion()

        display_name = profile.display_name or user.username
        avatar_url = signed_url_for_file(profile.avatar, request)
        cover_url = signed_url_for_file(profile.cover, request)

        interests = list(
            user.user_interests
            .select_related('interest')
            .order_by('interest__name')
            .values_list('interest__name', flat=True)
        ) or profile.interests

        data = {
            'user': {
                'id': user.id,
                'username': user.username,
                'effective_username': profile.effective_username,
                'display_name': display_name,
                'avatar_url': avatar_url,
                'cover_url': cover_url,
                'tagline': profile.tagline,
                'bio': profile.bio,
                'location': profile.location,
                'joined_at': user.date_joined.strftime('%Y-%m-%d') if hasattr(user, 'date_joined') else None,
                'subscribers_count': profile.subscribers_count,
            },
            'profile_completion': completion,
            'stats': {
                'available': False,
                'new_messages': 0,
                'subscriptions_count': 0,
                'watch_history_count': 0,
                'total_views': 0,
                'recommendations_count': 0,
                'completed_lessons': 0,
            },
            'awards': [],
            'interests': interests,
            'favorite_genres': profile.favorite_genres,
            'views_analytics': {
                'available': False,
                'period': '30d',
                'points': [],
                'summary': {
                    'views': 0,
                    'views_delta_percent': 0,
                    'unique_viewers': 0,
                    'unique_viewers_delta_percent': 0,
                    'average_watch_time': '',
                    'average_watch_time_delta_percent': 0,
                },
            },
            'recent_activity': [],
            'favorite_authors': [],
            'continue_watching': [],
            'settings': {
                'language': profile.language,
                'content_language': profile.content_language,
                'private_account': profile.private_account,
                'notifications_in_app': profile.notifications_in_app,
                'notifications_email': profile.notifications_email,
                'comment_permission': profile.comment_permission,
            },
        }
        return Response(data)


class ProfileSettingsView(APIView):
    def get(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            profile = _get_or_create_profile(user)
        except InactiveAccount:
            return _inactive_account_response()
        return Response(UserProfileSettingsSerializer(profile).data)

    def patch(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = UserProfileSettingsSerializer(
            data=request.data,
            partial=True,
        )
        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                locked_user = lock_active_user(user)
                profile, _ = (
                    UserProfile.objects.select_for_update().get_or_create(
                        user=locked_user,
                    )
                )
                for field, value in serializer.validated_data.items():
                    setattr(profile, field, value)
                profile.save()
                response_data = UserProfileSettingsSerializer(profile).data
        except InactiveAccount:
            return _inactive_account_response()
        return Response(response_data)


_BASIC_FIELDS = (
    'display_name', 'public_username', 'bio', 'tagline', 'location',
    'language', 'content_language', 'private_account',
    'notifications_in_app', 'notifications_email', 'comment_permission',
)


def _has_conflict_error(errors) -> bool:
    field_errors = errors.get('public_username') if isinstance(errors, dict) else None
    if not field_errors:
        return False
    for err in field_errors:
        code = getattr(err, 'code', None)
        if code == 'conflict':
            return True
    return False


class ProfileMeView(APIView):
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_throttles(self):
        if self.request.method == 'DELETE':
            return [AccountDeleteThrottle()]
        return super().get_throttles()

    def get(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            profile = _get_or_create_profile(user)
        except InactiveAccount:
            return _inactive_account_response()
        return Response(serialize_profile_me(profile, request))

    def patch(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

        serializer = ProfileMeUpdateSerializer(
            data=request.data, partial=True, context={'user': user}
        )
        if not serializer.is_valid():
            if _has_conflict_error(serializer.errors):
                return Response(
                    {
                        'detail': 'username already taken',
                        'field': 'public_username',
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                {'detail': 'validation error', 'errors': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                locked_user = lock_active_user(user)
                profile, _ = (
                    UserProfile.objects.select_for_update().get_or_create(
                        user=locked_user,
                    )
                )
                data = serializer.validated_data
                for field in _BASIC_FIELDS:
                    if field in data:
                        setattr(profile, field, data[field])
                profile.save()

                if 'interests' in data:
                    replace_user_interests(
                        locked_user,
                        data['interests'],
                    )
                if 'socials' in data:
                    replace_user_socials(locked_user, data['socials'])

                profile.refresh_from_db()
                response_data = serialize_profile_me(profile, request)
        except InactiveAccount:
            return _inactive_account_response()
        except IntegrityError:
            return Response(
                {'detail': 'username already taken', 'field': 'public_username'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(response_data)

    def delete(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response(
                {'detail': 'Unauthorized'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        current_password = request.data.get('current_password')
        if not isinstance(current_password, str) or not current_password.strip():
            return api_error_response(
                code='ACCOUNT_DELETE_PASSWORD_REQUIRED',
                message='current password is required',
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            close_user_account(user, current_password)
        except InvalidCurrentPassword:
            return api_error_response(
                code='ACCOUNT_DELETE_PASSWORD_INVALID',
                message='current password is invalid',
                status=status.HTTP_403_FORBIDDEN,
            )
        except AccountHasOwnedProjects as exc:
            message = (
                'transfer or delete owned projects before closing the account'
            )
            return Response(
                {
                    'error': {
                        'code': 'ACCOUNT_HAS_OWNED_PROJECTS',
                        'message': message,
                    },
                    'code': 'ACCOUNT_HAS_OWNED_PROJECTS',
                    'detail': message,
                    'ownedProjectCount': exc.owned_projects_count,
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)


class _ImageEndpointMixin:
    asset_type: str = ''

    def post(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        upload = request.FILES.get('file')
        if upload is None:
            return Response(
                {'detail': 'validation error', 'errors': {'file': ['this field is required']}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            _, profile = save_uploaded_image(user, upload, self.asset_type)
        except InactiveAccount:
            return _inactive_account_response()
        except UnsupportedMediaType as e:
            return Response(
                {'detail': f'unsupported media type: {e}'},
                status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )
        except FileTooLarge as e:
            return Response(
                {'detail': f'file too large: {e}'},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        return Response(serialize_profile_me(profile, request), status=status.HTTP_200_OK)

    def delete(self, request):
        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            profile = delete_image(user, self.asset_type)
        except InactiveAccount:
            return _inactive_account_response()
        return Response(serialize_profile_me(profile, request), status=status.HTTP_200_OK)


class ProfileAvatarView(_ImageEndpointMixin, APIView):
    parser_classes = [MultiPartParser, FormParser]
    asset_type = UserAsset.AVATAR


class ProfileCoverView(_ImageEndpointMixin, APIView):
    parser_classes = [MultiPartParser, FormParser]
    asset_type = UserAsset.COVER


class ImageModelView(APIView):
    """GET / PATCH the user's preferred image-generation model.

    Body shape for PATCH:
        {"image_generation_model": "gemini-flash-image"}    # set
        {"image_generation_model": null}                    # reset to default
    """

    parser_classes = [JSONParser, FormParser]

    def get(self, request):
        from types import SimpleNamespace

        from w_craft_back.auth.models import UserKey
        from w_craft_back.character_studio.services.errors import (
            CharacterStudioError,
        )
        from w_craft_back.character_studio.services.generation_lifecycle import (
            _legacy_model_spec,
            _provider_preference,
            resolve_character_provider,
        )
        from w_craft_back.movie.project import policy
        from w_craft_back.movie.project.models import Project
        from w_craft_back.services.image_generation import (
            deserialize_model_spec,
            list_available_models,
            model_catalog_row,
            resolve_model,
        )
        from w_craft_back.services.image_generation.errors import (
            CODE_NOT_CONFIGURED,
        )

        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            profile = _get_or_create_profile(user)
        except InactiveAccount:
            return _inactive_account_response()
        raw_project_id = request.query_params.get('project_id')
        if raw_project_id is None:
            return Response(self._serialize(profile))

        try:
            project_id = int(str(raw_project_id).strip())
        except (TypeError, ValueError):
            project_id = 0
        if project_id <= 0:
            return Response(
                {
                    'detail': 'validation error',
                    'errors': {'project_id': ['must be a positive integer']},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        project = Project.objects.filter(pk=project_id).first()
        if project is None:
            return Response({'detail': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        if not policy.can_view(user, project):
            return Response({'detail': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

        actor = UserKey.objects.filter(user=user).first()
        if actor is None:
            actor = SimpleNamespace(user=user)
        requested_key, requested_source = _provider_preference(
            project,
            actor,
            {},
        )
        try:
            selection = resolve_character_provider(
                project=project,
                actor=actor,
                request_payload={},
                provider_operation='generate',
            )
        except CharacterStudioError as exc:
            if exc.error_code != CODE_NOT_CONFIGURED:
                return Response(
                    {'detail': exc.message, 'code': exc.error_code},
                    status=exc.status_code,
                )
            normalized = (requested_key or '').lower()
            if normalized in {'mock', 'gemini', 'google', 'imagen'}:
                current = 'mock' if normalized == 'mock' else 'gemini'
                spec = _legacy_model_spec(current)
            else:
                spec = resolve_model(requested_key)
                current = spec.key
            source = requested_source
            configured = False
        else:
            spec = deserialize_model_spec(selection.snapshot['spec'])
            current = selection.key
            source = selection.source
            configured = True

        available = list_available_models()
        if not any(row['key'] == current for row in available):
            selected_row = model_catalog_row(spec)
            selected_row['configured'] = configured
            available.append(selected_row)
        return Response({
            'current': current,
            'source': source,
            'configured': configured,
            'stored': profile.image_generation_model or None,
            'available': available,
        })

    def patch(self, request):
        from w_craft_back.services.image_generation import resolve_model
        from w_craft_back.services.image_generation.errors import ImageProviderError

        user = _get_user_from_request(request)
        if user is None:
            return Response({'detail': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

        if 'image_generation_model' not in request.data:
            return Response(
                {'detail': 'validation error',
                 'errors': {'image_generation_model': ['this field is required']}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw = request.data.get('image_generation_model')
        if raw is None or raw == '':
            new_value = ''
        else:
            if not isinstance(raw, str):
                return Response(
                    {'detail': 'validation error',
                     'errors': {'image_generation_model': ['must be a string or null']}},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            key = raw.strip()
            try:
                spec = resolve_model(key)
            except ImageProviderError as exc:
                return Response(
                    {
                        'detail': exc.message,
                        'code': exc.code,
                        'field': 'image_generation_model',
                    },
                    status=exc.http_status,
                )
            if not spec.supports_generate:
                return Response(
                    {
                        'detail': 'image model cannot generate supported images',
                        'code': 'IMAGE_PROVIDER_GENERATE_NOT_SUPPORTED',
                        'field': 'image_generation_model',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            max_length = UserProfile._meta.get_field(
                'image_generation_model'
            ).max_length
            if max_length and len(key) > max_length:
                return Response(
                    {
                        'detail': 'image model key is too long',
                        'errors': {
                            'image_generation_model': [
                                f'must be at most {max_length} characters'
                            ]
                        },
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            new_value = key

        try:
            with transaction.atomic():
                locked_user = lock_active_user(user)
                profile, _ = (
                    UserProfile.objects.select_for_update().get_or_create(
                        user=locked_user,
                    )
                )
                profile.image_generation_model = new_value
                profile.save(
                    update_fields=['image_generation_model', 'updated_at'],
                )
                response_data = self._serialize(profile)
        except InactiveAccount:
            return _inactive_account_response()
        return Response(response_data)

    @staticmethod
    def _serialize(profile: UserProfile) -> dict:
        from w_craft_back.services.image_generation import list_available_models
        from w_craft_back.services.image_generation.resolver import (
            resolve_current_for_user,
        )

        current = resolve_current_for_user(profile.user)
        return {
            'current': current['key'],
            'source': current['source'],
            'configured': current['configured'],
            'stored': profile.image_generation_model or None,
            'available': list_available_models(),
        }
