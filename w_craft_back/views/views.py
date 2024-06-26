import base64
import io
import logging
import os
import requests


from django.http import HttpResponse
from dotenv import load_dotenv
from PIL import Image
from huggingface_hub.inference_api import InferenceApi
from rest_framework.views import APIView

from w_craft_back.generation.promt.builder import get_promt_age

# Load environment variables from .env file
load_dotenv()
TOKEN_HUGGING = os.getenv('TOKEN_HUGGING_FACE')
NVIDIA_KEY = os.getenv('NVIDIA_KEY')
STABLE_KEY = os.getenv('STABLE_KEY')

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
        style_gen = params.get('styleGen')

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


        prompt_global += f'. Style: {style_gen}.'
        logger.info(f'Prompt person: ${prompt_global}')

        image = create_image_from_string(prompt_global)
        response = img2response(image)

        return response


class GenerateImageUndefinedView(APIView):
    def get(self, request):
        params = request.GET

        desc = params.get('description', None)
        character = params.get('character', None)
        style_gen = params.get('styleGen')

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

        prompt_global += f'. Style: {style_gen}.'

        logger.info(f'Prompt undefined: ${prompt_global}')

        image = create_image_from_string(prompt_global)
        response = img2response(image)

        return response


class GenerateImg2ImgView(APIView):
    def get(self, request):
        logger.info('Request to image to image')
        params = request.GET

        url = params.get('url')
        prompt = params.get('prompt', '')
        character = params.get('character', None)
        style_gen = params.get('styleGen')

        prompt_global = 'Generate a movie poster one character by image. '
        prompt_global += prompt

        prompt_character = '' if character is None \
            else 'The appearance should reflect the character. ' \
                 'Personality: {}. '.format(character)
        prompt_global += prompt_character
        prompt_global += f'. Style: {style_gen}.'

        logger.info(f'Prompt img2img: ${prompt_global}')

        img_bytes = requests.get(url, stream=True)
        response = query_model_hub(img_bytes, prompt_global)

        return response


def query_model_hub(data, prompt):

    logger.info('Begin generate...')
    # repo_id = "stabilityai/stable-diffusion-xl-refiner-1.0"
    # repo_id: str = "stabilityai/stable-diffusion-xl-refiner-0.9"
    repo_id: str = 'instruction-tuning-sd/cartoonizer' # прикольная / делаем мультик
    inference = InferenceApi(repo_id=repo_id,
                             token=TOKEN_HUGGING)


    image = inference(data=data, inputs=prompt)
    if isinstance(image, dict) and 'error' in image.keys():
        logger.error('Model dont running!')
        logger.error(image['error'])
        return HttpResponse(status=500)

    logger.info(f'Image was generated with shape: {image.size}')

    response: HttpResponse = img2response(image)
    return response


def img2response(image):
    if isinstance(image, Image.Image):
        resized_image = image
    elif isinstance(image, dict):
        print(image.keys())
        f = image['b64_json']  # nvidia
        image_data = base64.b64decode(f)

        image = Image.open(io.BytesIO(image_data))
        resized_image = image.resize((500, 500))
        logger.info(resized_image.size)

    buffered = io.BytesIO()
    resized_image.save(buffered, format="PNG")
    f = base64.b64encode(buffered.getvalue()).decode('utf-8')

    # buffer: BytesIO = BytesIO()
    # image.save(buffer, format='PNG')
    # f = base64.b64encode(buffer.getvalue()).decode('utf-8')
    response: HttpResponse = HttpResponse(f, content_type='image/png')
    return response


def create_image_from_string(user_string):
    # Create a blank image
    # import time
    # time.sleep(3)
    # imarray = np.random.rand(100, 100, 3) * 255
    # image = Image.fromarray(imarray.astype('uint8')).convert('RGB')
    # return image
    logger.info('Begin generating...')

    invoke_url = "https://api.nvcf.nvidia.com/v2/nvcf/pexec/functions/89848fb8-549f-41bb-88cb-95d6597044a4"
    fetch_url_format = "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/"

    headers = {
        "Authorization": f"Bearer {NVIDIA_KEY}",
        "Accept": "application/json",
    }


    payload = {
        "prompt": user_string,
        "negative_prompt": "anime",
        "sampler": "DPM",
        "seed": 0,
        "guidance_scale": 5,
        "inference_steps": 25
    }

    session = requests.Session()

    response = session.post(invoke_url, headers=headers, json=payload)

    while response.status_code == 202:
        request_id = response.headers.get("NVCF-REQID")
        fetch_url = fetch_url_format + request_id
        response = session.get(fetch_url, headers=headers)

    response.raise_for_status()
    response_body = response.json()
    return response_body


    # logger.info('Begin generating...')
    # inference = InferenceApi(repo_id="stablediffusionapi/nightvision-xl-0791",
    #                          token=TOKEN_HUGGING)  # stabilityai/stable-diffusion-2
    # # inference = InferenceApi(repo_id="stabilityai/stable-diffusion-2")
    # output = inference(user_string)
    # logger.info(f'Image generated with shape: ${output.size}')
    # return output



