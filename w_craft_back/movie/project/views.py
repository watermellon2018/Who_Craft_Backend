from django.core.exceptions import ObjectDoesNotExist

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.models import Project, Genre, Audience

import base64
import logging

from django.http import JsonResponse, HttpResponse
from django.core.files.base import ContentFile
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.views import APIView


logger = logging.getLogger(__name__)


@api_view(['GET'])
def get_list_projects(request):
    user_token = request.GET.get('token_user')
    cur_user = UserKey.objects.get(key=user_token)
    logger.info(f'Пользователь {cur_user.key}')

    try:
        logger.info('Запрос на получение объектов')
        projects_list = Project.objects.filter(user=cur_user)

        logger.info('Объекты получены')
    except ObjectDoesNotExist:
        # Обработка случая, когда объект не найден
        logger.info('Проекты для пользователя не найдены')
        return JsonResponse([], safe=False, status=200)

    def build_project_list(proj):
        try:
            with open(proj.image.path, "rb") as img_file:
                img_obj = base64.b64encode(img_file.read()).decode('utf-8')
        except ValueError:
            img_obj = None

        response = {
            'id': proj.id,
            'title': proj.title,
            'src': img_obj,
        }
        return response

    data = [build_project_list(proj) for proj in projects_list]
    logger.info('Количество проектов: {}'.format(len(data)))
    return JsonResponse(data, safe=False, status=200)


@api_view(['GET'])
def delete_project(request):
    user_token = request.GET.get('token_user')
    cur_user = UserKey.objects.get(key=user_token)


    try:
        logger.info('Удаление проекта')
        id = request.GET.get('id')
        project = Project.objects.get(id=id, user=cur_user)
        project.delete()
        logger.info('Проект удален!')

    except Project.DoesNotExist:
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    return HttpResponse(status=status.HTTP_200_OK)


@api_view(['GET'])
def select_project_info(request):
    user_token = request.GET.get('token_user')
    cur_user = UserKey.objects.get(key=user_token)


    try:
        logger.info('Select запрос проекта')
        id = request.GET.get('id')
        project = Project.objects.get(id=id, user=cur_user)
        logger.info(f'Проект найден id: {id}')
        img_obj = None
        if not project.image == '':
            with open(project.image.path, "rb") as img_file:
                img_obj = base64.b64encode(img_file.read()).decode('utf-8')
                logger.info('Постер найден')

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

    except Project.DoesNotExist:
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
def update_info_project(request):
    logger.info('Обновить информацию о проекте')
    data = request.data['data']
    user_token = data['token_user']
    cur_user = UserKey.objects.get(key=user_token)

    try:
        id = data['id']
        project = Project.objects.get(id=id, user=cur_user)
        logger.info(f'Проект найден id: {id}')

        if not data['image'] == '':
            title = data['title']
            logger.info('Пользователь загрузил постер для своего проекта')
            image_data = data['image']
            format, imgstr = image_data.split(';base64,')
            ext = format.split('/')[-1]
            image_data = ContentFile(base64.b64decode(imgstr),
                                     name='{}.{}'.format(title, ext))  # TODO:: add user id!
            project.src = image_data

        project.title = data['title']
        project.format = data['format']
        project.annot = data['annot']
        project.desc = data['desc']

        audience_list: list = data['audience']
        audience_objs = Audience.objects.filter(name__in=audience_list)
        project.audience.set(audience_objs)

        genre_list: list = data['genre']
        print(genre_list)
        genre_objs = Genre.objects.filter(translit__in=genre_list)
        project.genre.set(genre_objs)

        project.save()

        return HttpResponse(status=200)

    except Project.DoesNotExist:
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


class ProjectView(APIView):

    def __init__(self):
        super().__init__()
        self.format_choices = ['full-movie', 'short-movie', 'series', 'marketing']

    def post(self, request):
        logger.info('Создаем проект')
        data = request.data['data']

        user_token = data['token_user']
        cur_user = UserKey.objects.get(key=user_token)
        logger.info(cur_user.user)

        title: str = data['title']
        genre_list: list = data['genre']
        audience_list: list = data['audience']
        format: str = data['format']
        desc: str = data['desc']
        annot: str = data['annot']

        if format not in self.format_choices:
            logger.error('Пользователь ввел странный формат фильма!!!')
            return HttpResponse(status=status.HTTP_400_BAD_REQUEST,
                                reason='Некорректный тип формата')

        arguments = {'title': title,
                     'format': format,
                     'annot': annot,
                     'desc': desc,
                     'user': cur_user,
                     }
        if not data['image'] == '':
            logger.info('Пользователь загрузил постер для своего проекта')
            image_data = data['image']
            format, imgstr = image_data.split(';base64,')
            ext = format.split('/')[-1]
            image_data = ContentFile(base64.b64decode(imgstr),
                                     name='{}.{}'.format(title, ext))  # TODO:: add user id!
            arguments['image'] = image_data

        obj = Project.objects.create(**arguments)

        audience_objs = Audience.objects.filter(name__in=audience_list)
        obj.audience.set(audience_objs)

        genre_objs = Genre.objects.filter(translit__in=genre_list)
        obj.genre.set(genre_objs)

        logger.info('Проект создан!')
        return JsonResponse({'project_id': obj.id}, status=200)
