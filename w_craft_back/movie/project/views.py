from w_craft_back.movie.properties.utils import translit
import base64
from django.core.files.base import ContentFile
import logging

from rest_framework.views import APIView

from w_craft_back.movie.project.models import Project, Genre, Audience
from django.http import JsonResponse, HttpResponse
from rest_framework import status

logger = logging.getLogger(__name__)


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
                                     name='{}.{}'.format(title, ext)) # TODO:: add user id!
            arguments['image'] = image_data


        obj = Project.objects.create(**arguments)

        genre_objs = Genre.objects.filter(translit__in=genre_list)
        obj.genre.set(genre_objs)

        audience_objs = Audience.objects.filter(name__in=audience_list)
        obj.audience.set(audience_objs)

        logger.info('Проект создан!')

        return HttpResponse(status=status.HTTP_200_OK)
