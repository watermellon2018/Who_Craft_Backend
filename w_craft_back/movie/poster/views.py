
import base64
import logging

from django.http import JsonResponse, HttpResponse
from django.core.files.base import ContentFile
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.views import APIView

from w_craft_back.views import create_image_from_string, img2response

logger = logging.getLogger(__name__)
@api_view(['GET'])
def generate_poster(request):
    params = request.GET

    desc = params.get('description', None)

    prompt_global = 'Generate a movie poster. Description:'

    prompt_desc = '' if desc is None else 'Description: {}. '.format(desc)
    prompt_global += prompt_desc

    logger.info(f'Prompt gen poster: ${prompt_global}')

    image = create_image_from_string(prompt_global)
    response = img2response(image)

    return response