from rest_framework import status

from w_craft_back.characters.creating.utils import check_exist_project
from w_craft_back.models import MenuFolder

import base64
import logging

from django.core.files.base import ContentFile
from django.forms.models import model_to_dict
from django.http import JsonResponse, HttpResponse
from rest_framework.decorators import api_view
from w_craft_back.movie.project.models import Project
from w_craft_back.characters.creating.models import Character, \
    GoalsMotivation, BiographyRelationships, PersonalityTraits, \
    ProfessionHobbies, TalentsAbilities

logger = logging.getLogger(__name__)


# изменить параметры


# @api_view(['GET'])
# def delete_by_project(request):
#     try:
#         logger.info('Удаление персонажа по id')
#
#         project_id = request.GET.get('projectId')
#         cur_project = Project.objects.get(id=project_id)
#         heroes_to_delete = Character.objects.filter(project=cur_project)
#         heroes_to_delete.delete()
#
#         logger.info('Персонаж удален')
#
#
#     except Project.DoesNotExist:
#         logger.error('Проект для которого создается персонаж, не найден')
#         return JsonResponse(
#             {'error': 'Object with specified ID does not exist'},
#             status=404)
#
#     except Exception as e:
#         return JsonResponse({'error': str(e)}, status=500)
#
#     return HttpResponse(status=status.HTTP_200_OK)


@api_view(['GET'])
def delete_hero_by_id(request):
    try:
        logger.info('Удаление персонажа по id')

        try:
            project_id = request.GET.get('projectId')
            cur_project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            logger.error('Проект не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        character_id = request.GET.get('characterId')
        hero = Character.objects.get(project=cur_project, id=character_id)
        hero.delete()
        logger.info('Персонаж удален')

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    return HttpResponse(status=status.HTTP_200_OK)


@api_view(['GET'])
def select_hero_by_id(request):
    try:
        logger.info('Возвращаем персонажа по id')
        try:
            project_id = request.GET.get('projectId')
            cur_project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            logger.error('Проект не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        character_id = request.GET.get('characterId')
        hero = Character.objects.get(project=cur_project, id=character_id)

        resp = {
            'name': hero.first_name,
            'lastName': hero.last_name,
            'middleName': hero.middle_name,
            'dob': hero.birth_date,
            'town': hero.birth_place
        }
        logger.info('Достаем изображение')

        try:
            with open(hero.photo.path, "rb") as img_file:
                img_obj = base64.b64encode(img_file.read()).decode('utf-8')
        except ValueError:
            img_obj = None
        resp['image'] = img_obj

        logger.info('Изображение вытащено')

        data = GoalsMotivation.objects.get(character=hero)
        resp['forWhat'] = data.purpose_in_story
        resp['goal'] = data.goal
        resp['philosophy'] = data.life_philosophy
        resp['development'] = data.character_development
        logger.info('Цели и мотивация вытащены')

        data = BiographyRelationships.objects.get(character=hero)
        resp['biography'] = data.biography
        resp['relationship'] = data.relationships_with_others
        resp['additInfo'] = data.addit_info
        logger.info('Биография и доп информация вытащены')

        data = PersonalityTraits.objects.get(character=hero)
        resp['character'] = data.character_type
        resp['personalTraits'] = data.personal_traits
        resp['strengthsWeaknesses'] = data.strengths_weaknesses
        resp['complexs'] = data.complexes
        resp['insideConflict'] = data.inner_conflicts
        resp['style'] = data.individual_style
        logger.info('Личные качества вытащены')

        data = ProfessionHobbies.objects.get(character=hero)
        resp['profession'] = data.profession
        resp['hobby'] = data.hobbies
        logger.info('Увлечения вытащены')

        data = TalentsAbilities.objects.get(character=hero)
        resp['talents'] = data.talents
        resp['mindInfo'] = data.intellectual_abilities
        resp['sportInfo'] = data.physical_characteristics
        resp['appearance'] = data.external_characteristics
        resp['speech'] = data.speech_patterns
        logger.info('Внутренние характерисики вытащены')

        logger.info('Персонаж найден')

        return JsonResponse(resp, safe=False, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['GET'])
def select_all(request):
    try:
        logger.info('Возвращаем всех персонаей проекта')

        try:
            project_id = request.GET.get('projectId')
            cur_project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            logger.error('Проект не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        objs = Character.objects.filter(project=cur_project)

        def build_response(hero):
            hero_serls = model_to_dict(hero)
            logger.info(hero_serls)
            title = hero_serls['first_name'] + ' ' + hero_serls['last_name']

            try:
                with open(hero_serls['photo'].path, "rb") as img_file:
                    img_obj = base64.b64encode(img_file.read()).decode('utf-8')
            except ValueError:
                img_obj = None

            response = {
                'id': str(hero_serls['id']),
                'key': hero_serls['first_name'] + '_' + str(hero_serls['id']),
                'name': title,
                'src': img_obj
            }
            logger.info(response)
            return response

        heros = [build_response(hero) for hero in objs]
        if len(heros) == 0:
            logger.info('В данном проекте отсутствуют персонажи')
        else:
            logger.info('Персонажи найдены')

        return JsonResponse(heros, safe=False, status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['GET'])
def select_by_id(request):
    try:
        logger.info('Вывод информации о персонаже')

        hero_id = request.GET.get('heroId')
        obj = Character.objects.get(id=hero_id)
        logger.info('Объект получен.')

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse(obj, safe=False, status=200)




@api_view(['POST'])
def create_hero(request):
    try:
        logger.info('Задаем настройки нового персонажа, которого хотим создать')
        params = request.data['data']

        try:
            project_id = params['projectId']
            cur_project = Project.objects.get(id=project_id)
            logger.info(f'Проект найден {project_id}')
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        name_project = cur_project.title
        user_id = cur_project.user_id

        argument = {'project': cur_project}

        name_hero = params['name']

        if name_hero is None or name_hero == '':
            logger.error('Не указано имя героя')
            raise JsonResponse({'error': 'Не указано имя героя'}, status=500)

        argument['first_name'] = name_hero
        argument['last_name'] = params['lastName']
        argument['middle_name'] = params['middleName']
        argument['birth_date'] = params['dob']
        argument['birth_place'] = params['town']

        logger.info('Личные настройки указаны')

        obj = Character.objects.create(**argument)

        argument = {'character': obj,
                    'purpose_in_story': params['forWhat'],
                    'goal': params['goal'],
                    'life_philosophy': params['philosophy'],
                    'character_development': params['develop']}

        GoalsMotivation.objects.create(**argument)
        logger.info('Объект мотивации создан')

        argument = {'character': obj,
                    'personal_traits': params['personalTraits'],
                    'strengths_weaknesses': params['strengthsWeaknesses'],
                    'character_type': params['character'],
                    'complexes': params['complexes'],
                    'inner_conflicts': params['insideConflict'],
                    'individual_style': params['style']}

        # личностные особенности
        PersonalityTraits.objects.create(**argument)
        logger.info('Объект личностные особенности создан')

        argument = {'character': obj,
                    'biography': params['biography'],
                    'relationships_with_others': params['relationship'],
                    'addit_info': params['additInfo'],
                    }
        BiographyRelationships.objects.create(**argument)
        logger.info('Объект биографии создан')

        argument = {'character': obj,
                    'profession': params['profession'],
                    'hobbies': params['hobby']}
        ProfessionHobbies.objects.create(**argument)
        logger.info('Объект увлечений создан')

        argument = {'character': obj,
                    'talents': params['talents'],
                    'intellectual_abilities': params['mindInfo'],
                    'physical_characteristics': params['sportInfo'],
                    'external_characteristics': params['appearance'],
                    'speech_patterns': params['speech']}
        TalentsAbilities.objects.create(**argument)

        logger.info('Объект физических характеристик создан')
        logger.info('Второстепенные настройки персонажа зарегестрированы')

        argument = {'project': cur_project,
                    'photo': '',
                    'first_name': name_hero,
                    'last_name': params['lastName'],
                    'middle_name': params['middleName'],
                    'birth_date': params['dob'],
                    'birth_place': params['town']
                    }

        image_data = params['image']
        if image_data is None or image_data == '':
            logger.error('Нет постера для проекта')
        if ';base64,' not in image_data:
            logger.error('Это не изображение')
        else:
            format, imgstr = image_data.split(';base64,')
            ext = format.split('/')[-1]
            argument['photo'] = ContentFile(base64.b64decode(imgstr),
                                            name='{}/{}/{}/{}'.format(user_id,
                                                                      name_project,
                                                                      name_hero,
                                                                      ext))
            logger.info('Изображение загружено')

        Character.objects.create(**argument)
        logger.info('Персонаж создан')

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse({'message': 'Object created successfully'},
                        status=200)
