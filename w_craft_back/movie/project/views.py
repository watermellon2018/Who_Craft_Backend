import base64
import logging
import os
import uuid

from django.core.exceptions import ObjectDoesNotExist
from django.core.files.base import ContentFile
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.http import HttpResponse, JsonResponse
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.views import APIView

from w_craft_back.auth.utils import resolve_user_key
from w_craft_back.movie.project.models import Audience, Genre, Project

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
        projects_list = Project.objects.filter(user=cur_user).select_related('user')
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
        project = Project.objects.get(id=project_id, user=cur_user)
        project.delete()
    except (Project.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)
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
            .get(id=project_id, user=cur_user)
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
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)
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
        cur_user = resolve_user_key(request)
    except AuthenticationFailed:
        return _auth_failed_response()

    data = request.data.get('data') if isinstance(request.data, dict) else None
    if not isinstance(data, dict):
        return JsonResponse({'error': 'invalid_payload'}, status=400)

    project_id = data.get('id')
    try:
        project = Project.objects.get(id=project_id, user=cur_user)

        image_data = data.get('image') or ''
        if image_data:
            old_photo = project.image
            if old_photo:
                old_photo.delete()

            title = data.get('title') or ''
            try:
                fmt, imgstr = image_data.split(';base64,')
            except ValueError:
                return JsonResponse({'error': 'invalid_image_payload'}, status=400)
            ext = fmt.split('/')[-1]

            user_id = project.user_id
            unique_id = uuid.uuid4()
            path = f'{user_id}/{title}/{unique_id}.{ext}'

            try:
                decoded = base64.b64decode(imgstr)
            except Exception:
                return JsonResponse({'error': 'invalid_image_payload'}, status=400)
            project.image = ContentFile(decoded, name=path)

        project.title = data.get('title', project.title)
        project.format = data.get('format', project.format)
        project.annot = data.get('annot', project.annot)
        project.desc = data.get('desc', project.desc)

        audience_list = data.get('audience') or []
        if isinstance(audience_list, list):
            project.audience.set(Audience.objects.filter(name__in=audience_list))

        genre_list = data.get('genre') or []
        if isinstance(genre_list, list):
            project.genre.set(Genre.objects.filter(translit__in=genre_list))

        project.save()
        return HttpResponse(status=200)

    except (Project.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)
    except Exception:
        logger.exception('update_info_project failed for project_id=%s', project_id)
        return JsonResponse({'error': 'internal_error'}, status=500)


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
        }
        image_data = data.get('image') or ''
        if image_data:
            try:
                img_fmt, imgstr = image_data.split(';base64,')
            except ValueError:
                return JsonResponse({'error': 'invalid_image_payload'}, status=400)
            ext = img_fmt.split('/')[-1]
            unique_id = uuid.uuid4()
            path = f'{cur_user.id}/{title}/{unique_id}.{ext}'
            try:
                decoded = base64.b64decode(imgstr)
            except Exception:
                return JsonResponse({'error': 'invalid_image_payload'}, status=400)
            arguments['image'] = ContentFile(decoded, name=path)

        try:
            obj = Project.objects.create(**arguments)
            if isinstance(audience_list, list):
                obj.audience.set(Audience.objects.filter(name__in=audience_list))
            if isinstance(genre_list, list):
                obj.genre.set(Genre.objects.filter(translit__in=genre_list))
        except Exception:
            logger.exception('Project create failed')
            return JsonResponse({'error': 'internal_error'}, status=500)

        return JsonResponse({'project_id': obj.id}, status=200)
