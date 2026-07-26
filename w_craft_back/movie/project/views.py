import base64
import logging
import os

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.http import HttpResponse, JsonResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.views import APIView

from w_craft_back.auth.utils import resolve_user_key
from w_craft_back.movie.project import (
    policy,
    project_mutations,
    team_errors,
    team_service,
)
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Audience, Genre, Project
from w_craft_back.movie.project.project_images import (
    decode_project_image_data_url,
)

logger = logging.getLogger(__name__)


def _auth_failed_response():
    return JsonResponse({'error': 'authentication_failed'}, status=401)


@api_view(['GET'])
def get_list_projects(request):
    try:
        cur_user = resolve_user_key(request)
    except AuthenticationFailed:
        return _auth_failed_response()

    try:
        projects_list = (
            Project.objects.filter(owner=cur_user.user).select_related("owner")
        )
    except ObjectDoesNotExist:
        return JsonResponse([], safe=False, status=200)

    def build_project_list(proj):
        try:
            with open(proj.image.path, "rb") as img_file:
                img_obj = base64.b64encode(img_file.read()).decode('utf-8')
        except (ValueError, OSError):
            img_obj = None

        return {
            'id': proj.id,
            'title': proj.title,
            'src': img_obj,
        }

    data = [build_project_list(proj) for proj in projects_list]
    return JsonResponse(data, safe=False, status=200)


@api_view(['DELETE', 'POST'])
def delete_project(request):
    """Delete a project. Accepts DELETE (preferred) or POST for back-compat.

    GET is rejected: state-mutating requests over GET are CSRF-vulnerable and
    can be triggered by image tags, prefetchers, or browser history replay.
    """
    try:
        cur_user = resolve_user_key(request)
    except AuthenticationFailed:
        return _auth_failed_response()

    project_id = (
        request.data.get('id') if isinstance(request.data, dict) else None
    ) or request.GET.get('id')

    try:
        team_service.delete_project(cur_user.user, project_id)
    except (
        Project.DoesNotExist,
        team_errors.InsufficientPermissions,
        ValueError,
        TypeError,
    ):
        return JsonResponse(
            {'error': 'Object with specified ID does not exist'},
            status=404,
        )
    except Exception:
        logger.exception('delete_project failed for project_id=%s', project_id)
        return JsonResponse({'error': 'internal_error'}, status=500)

    return HttpResponse(status=status.HTTP_200_OK)


@api_view(['GET'])
def select_project_info(request):
    try:
        cur_user = resolve_user_key(request)
    except AuthenticationFailed:
        return _auth_failed_response()

    project_id = request.GET.get('id')
    try:
        project = (
            Project.objects
            .prefetch_related('genre', 'audience')
            .get(id=project_id, owner=cur_user.user)
        )
        img_obj = None
        if project.image:
            try:
                with open(project.image.path, "rb") as img_file:
                    img_obj = base64.b64encode(img_file.read()).decode('utf-8')
            except (ValueError, OSError):
                img_obj = None

        response = {
            'id': project.id,
            'title': project.title,
            'genre': [genre.translit for genre in project.genre.all()],
            'format': project.format,
            'audience': [aud.name for aud in project.audience.all()],
            'annot': project.annot,
            'desc': project.desc,
            'src': img_obj,
        }
        return JsonResponse(response, safe=False, status=200)

    except (Project.DoesNotExist, ValueError, TypeError):
        return JsonResponse(
            {'error': 'Object with specified ID does not exist'},
            status=404,
        )
    except Exception:
        logger.exception('select_project_info failed for project_id=%s', project_id)
        return JsonResponse({'error': 'internal_error'}, status=500)


@receiver(pre_delete, sender=Project)
def delete_related_file(sender, instance, **kwargs):
    if not instance.image:
        return
    try:
        directory_path = os.path.dirname(instance.image.path)
    except ValueError:
        return
    instance.image.delete(False)
    if os.path.exists(directory_path) and len(os.listdir(directory_path)) == 0:
        try:
            os.rmdir(directory_path)
        except OSError:
            logger.warning('Failed to rmdir empty project dir: %s', directory_path)


@api_view(['POST'])
def update_info_project(request):
    try:
        credential = resolve_user_key(request)
    except AuthenticationFailed:
        return _auth_failed_response()

    data = request.data.get('data') if isinstance(request.data, dict) else None
    if not isinstance(data, dict):
        return JsonResponse({'error': 'invalid_payload'}, status=400)

    project_id = data.get('id')
    try:
        project_mutations.get_project_for_action(
            actor=credential.user,
            project_id=project_id,
            action=policy.Action.EDIT_SETTINGS,
        )
    except Project.DoesNotExist:
        return JsonResponse(
            {'error': 'Object with specified ID does not exist'},
            status=404,
        )
    except project_mutations.ProjectMutationForbidden:
        return JsonResponse({'error': 'forbidden'}, status=403)

    changes = {}
    field_map = {
        'title': 'title',
        'format': 'format',
        'annot': 'annotation',
        'desc': 'synopsis',
    }
    for legacy_name, canonical_name in field_map.items():
        if legacy_name in data:
            changes[canonical_name] = data[legacy_name]

    audience_list = data.get('audience')
    audiences = None
    if isinstance(audience_list, list):
        changes['audience'] = audience_list
        audiences = list(Audience.objects.filter(name__in=audience_list))

    genre_list = data.get('genre')
    genres = None
    if isinstance(genre_list, list):
        changes['genre'] = genre_list
        genres = list(Genre.objects.filter(translit__in=genre_list))

    poster_file = None
    poster_supplied = False
    image_data = data.get('image') or ''
    if image_data:
        poster_file = decode_project_image_data_url(
            image_data,
            owner_id=credential.user_id,
            title=data.get('title') or 'project',
        )
        if poster_file is None:
            return JsonResponse({'error': 'invalid_image_payload'}, status=400)
        poster_supplied = True

    try:
        project_mutations.update_project_settings(
            actor=credential.user,
            action=policy.Action.EDIT_SETTINGS,
            project_id=project_id,
            data=changes,
            genres=genres,
            audiences=audiences,
            poster_file=poster_file,
            poster_supplied=poster_supplied,
        )
    except Project.DoesNotExist:
        return JsonResponse(
            {'error': 'Object with specified ID does not exist'},
            status=404,
        )
    except project_mutations.ProjectMutationForbidden:
        return JsonResponse({'error': 'forbidden'}, status=403)
    except ValidationError as exc:
        return JsonResponse(
            {'error': 'invalid_payload', 'details': exc.message_dict},
            status=400,
        )
    return HttpResponse(status=200)


class ProjectView(APIView):
    format_choices = ('full-movie', 'short-movie', 'series', 'marketing')

    def post(self, request):
        try:
            cur_user = resolve_user_key(request)
        except AuthenticationFailed:
            return _auth_failed_response()

        data = request.data.get('data') if isinstance(request.data, dict) else None
        if not isinstance(data, dict):
            return JsonResponse({'error': 'invalid_payload'}, status=400)

        title = data.get('title') or ''
        genre_list = data.get('genre') or []
        audience_list = data.get('audience') or []
        fmt = data.get('format') or ''
        desc = data.get('desc') or ''
        annot = data.get('annot') or ''

        if fmt not in self.format_choices:
            return HttpResponse(status=status.HTTP_400_BAD_REQUEST,
                                reason='Некорректный тип формата')
        if not title.strip():
            return JsonResponse({'error': 'title_required'}, status=400)

        arguments = {
            'title': title,
            'format': fmt,
            'annot': annot,
            'desc': desc,
            'user': cur_user,
            'owner': cur_user.user,
        }
        image_data = data.get('image') or ''
        if image_data:
            image = decode_project_image_data_url(
                image_data,
                owner_id=cur_user.user_id,
                title=title,
            )
            if image is None:
                return JsonResponse(
                    {'error': 'invalid_image_payload'},
                    status=400,
                )
            arguments['image'] = image

        try:
            with transaction.atomic():
                obj = Project.objects.create(**arguments)
                ProjectMember.objects.create(
                    project=obj,
                    user=cur_user.user,
                    role=ProjectMemberRole.OWNER,
                )
                if isinstance(audience_list, list):
                    obj.audience.set(Audience.objects.filter(name__in=audience_list))
                if isinstance(genre_list, list):
                    obj.genre.set(Genre.objects.filter(translit__in=genre_list))
        except Exception:
            logger.exception('Project create failed')
            return JsonResponse({'error': 'internal_error'}, status=500)

        return JsonResponse({'project_id': obj.id}, status=200)
