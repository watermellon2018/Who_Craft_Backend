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
    try:
        # todo:: add user id
        logger.info('Изменение имени персонажа')
        projects_list = Project.objects.all()
        logger.info('Объекты получены')

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

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
    try:
        logger.info('Удаление проекта')
        id = request.GET.get('id')
        # todo:: add user id
        project = Project.objects.get(id=id)
        project.delete()
        logger.info('Проект удален!')

    except Project.DoesNotExist:
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    return HttpResponse(status=status.HTTP_200_OK)


class ProjectView(APIView):

    def __init__(self):
        super().__init__()
        self.format_choices = ['full-movie', 'short-movie', 'series', 'marketing']

    def post(self, request):
        logger.info('Создаем проект')
        data = request.data['data']

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
                     }
        if 'image' in data.keys():
            logger.info('Пользователь загрузил постер для своего проекта')
            image_data = data['image']
            format, imgstr = image_data.split(';base64,')
            ext = format.split('/')[-1]
            image_data = ContentFile(base64.b64decode(imgstr),
                                     name='{}.{}'.format(title, ext))  # TODO:: add user id!
            arguments['image'] = image_data

        obj = Project.objects.create(**arguments)

        genre_objs = Genre.objects.filter(translit__in=genre_list)
        obj.genre.set(genre_objs)

        audience_objs = Audience.objects.filter(name__in=audience_list)
        obj.audience.set(audience_objs)

        logger.info('Проект создан!')

        return HttpResponse(status=status.HTTP_200_OK)
