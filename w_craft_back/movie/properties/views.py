import logging

from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from w_craft_back.movie.properties.models import Genre
from django.http import JsonResponse

logger = logging.getLogger(__name__)


class GenreView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        logger.info('Запрашиваем список всех жанров кино')
        items = Genre.objects.all()

        def build_json(node):
            response = {
                'key': 'genre_movie_' + node.name,
                'value': node.translit,
                'name': node.name,
            }

            return response

        genres_json = [build_json(node) for node in items]
        return JsonResponse(genres_json, safe=False)
