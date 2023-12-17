import base64
import logging
import os

from django.http import HttpResponse
from dotenv import load_dotenv
from io import BytesIO
from huggingface_hub.inference_api import InferenceApi
from rest_framework.views import APIView

from w_craft_back.generation.promt.builder import get_promt_age

# Load environment variables from .env file
load_dotenv()
TOKEN_HUGGING = os.getenv('TOKEN_HUGGING_FACE')

logger = logging.getLogger(__name__)


def insert_substring(main_string, substring, index):
    return main_string[:index] + substring + main_string[index:]


class GenerateImageView(APIView):
    def get(self, request):
        params = request.GET

        gender = params.get('gender', None)
        min_value = params.get('minAge', None)
        max_value = params.get('maxAge', None)
        eyes = params.get('eyes', None)
        hair = params.get('hair', None)
        body = params.get('body', None)
        appearance = params.get('appearance', None)
        character = params.get('character', None)

        prompt_global = 'Generate a movie poster one character'
        begin_len_prompt = len(prompt_global)

        prompt_gender = '' if gender is None else 'Gender: {}. '.format(gender)
        prompt_global += prompt_gender

        prompt_age: str = get_promt_age(min_value, max_value)
        prompt_global += prompt_age

        prompt_eyes = '' if eyes is None else 'Eyes: {}. '.format(eyes)
        prompt_global += prompt_eyes

        prompt_hair = '' if hair is None else 'Hair: {}. '.format(hair)
        prompt_global += prompt_hair

        prompt_body = '' if body is None else 'Physique: {}. '.format(body)
        prompt_global += prompt_body

        prompt_appearance = '' if appearance is None \
            else 'Appearance: {}. '.format(appearance)
        prompt_global += prompt_appearance

        prompt_character = '' if character is None \
            else 'The appearance should reflect the character. ' \
                 'Personality: {}. '.format(character)
        prompt_global += prompt_character

        end_len_promt = len(prompt_global)

        if begin_len_prompt < end_len_promt:
            substring = ' with the following details. '
            prompt_global = prompt_global[:begin_len_prompt] + \
                substring + prompt_global[begin_len_prompt:]

        logger.info(f'Prompt person: ${prompt_global}')

        response = process_image(prompt_global)

        return response


class GenerateImageUndefinedView(APIView):
    def get(self, request):
        params = request.GET

        desc = params.get('description', None)
        character = params.get('character', None)

        prompt_global = 'Generate a movie poster one character'
        begin_len_prompt = len(prompt_global)

        prompt_desc = '' if desc is None else 'Description: {}. '.format(desc)
        prompt_global += prompt_desc

        prompt_character = '' if character is None \
            else 'The appearance should reflect the character. ' \
                 'Personality: {}. '.format(character)
        prompt_global += prompt_character

        end_len_prompt = len(prompt_global)

        if begin_len_prompt < end_len_prompt:
            substring = ' with the following details. '
            prompt_global = prompt_global[:begin_len_prompt] + \
                substring + prompt_global[begin_len_prompt:]

        logger.info(f'Prompt undefined: ${prompt_global}')

        response = process_image(prompt_global)

        return response


def process_image(promt: str):
    image = create_image_from_string(promt)

    buffer = BytesIO()
    image.save(buffer, format='PNG')
    f = base64.b64encode(buffer.getvalue()).decode('utf-8')
    response = HttpResponse(f, content_type='image/png')

    return response


def create_image_from_string(user_string):
    # Create a blank image
    # import time
    # time.sleep(3)
    # imarray = np.random.rand(100, 100, 3) * 255
    # image = Image.fromarray(imarray.astype('uint8')).convert('RGB')
    # return image

    logger.info('Begin generating...')
    inference = InferenceApi(repo_id="stablediffusionapi/nightvision-xl-0791",
                             token=TOKEN_HUGGING)  # stabilityai/stable-diffusion-2
    # inference = InferenceApi(repo_id="stabilityai/stable-diffusion-2")
    output = inference(user_string)
    logger.info(f'Image generated with shape: ${output.size}')
    return output
