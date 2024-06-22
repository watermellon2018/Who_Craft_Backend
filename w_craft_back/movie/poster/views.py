
import base64
import logging
import requests

from django.http import JsonResponse, HttpResponse
from django.core.files.base import ContentFile
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.views import APIView

from w_craft_back.views import create_image_from_string, img2response, query_model_hub

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


@api_view(['POST'])
def edite_generative_poster(request):
    params = request.data['data']

    desc = params['correction']
    img_url = params['image']
    logger.info('Param cut')

    prompt_global = 'Edit this input image according to the following text description, ' \
                    'while preserving the overall look and feel of the original image: '

    prompt_desc = desc
    prompt_global += prompt_desc

    logger.info(f'Prompt edit generative poster: ${prompt_global}')

    img_url = img_url.replace('data:image/png;base64,', '')
    img_bytes = base64.b64decode(img_url)

    response = query_model_hub(img_bytes, prompt_global)
    if response.status_code == 200:
        logger.info('Image was edited.')

    return response
