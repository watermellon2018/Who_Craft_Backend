from w_craft_back.movie.properties.utils import translit
import logging


from rest_framework.views import APIView

from w_craft_back.movie.properties.models import Genre
from django.http import JsonResponse

logger = logging.getLogger(__name__)

class GenreView(APIView):
    def get(self, request):
        logger.info('Запрашиваем список всех жанров кино')
        items = Genre.objects.all()
        def build_json(node):
            value = translit(node.name)
            response = {
                'key': 'genre_movie_' + node.name,
                'value': value,
                'name': node.name,
            }

            return response

        genres_json = [build_json(node) for node in items]
        return JsonResponse(genres_json, safe=False)
