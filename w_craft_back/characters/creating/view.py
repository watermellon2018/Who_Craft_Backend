from rest_framework import status

from w_craft_back.models import MenuFolder

import base64
import logging

from django.core.files.base import ContentFile
from django.http import JsonResponse, HttpResponse
from rest_framework.decorators import api_view
from w_craft_back.movie.project.models import Project
from w_craft_back.characters.creating.models import Character, \
    GoalsMotivation, BiographyRelationships, PersonalityTraits, \
    ProfessionHobbies, TalentsAbilities

logger = logging.getLogger(__name__)

# изменить параметры


@api_view(['GET'])
def delete_by_project(request):
    try:
        logger.info('Удаление персонажа по id')

        project_id = request.GET.get('projectId')
        cur_project = Project.objects.get(id=project_id)
        heroes_to_delete = Character.objects.filter(project=cur_project)
        heroes_to_delete.delete()

        logger.info('Персонаж удален')


    except Project.DoesNotExist:
        logger.error('Проект для которого создается персонаж, не найден')
        return JsonResponse(
            {'error': 'Object with specified ID does not exist'},
            status=404)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    return HttpResponse(status=status.HTTP_200_OK)

@api_view(['GET'])
def delete_by_id(request):
    try:
        logger.info('Удаление персонажа по id')
        hero_id = request.GET.get('heroId')
        hero = Character.objects.get(id=hero_id)
        hero.delete()
        logger.info('Персонаж удален')

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    return HttpResponse(status=status.HTTP_200_OK)

@api_view(['GET'])
def select_all(request):
    try:
        logger.info('Возвращаем всех персонаей проекта')
        try:
            project_id = request.GET.get('projectId')
            cur_project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        obj = Character.objects.filter(project=cur_project)
        logger.info('Персонажи найдены')


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse(obj, safe=False, status=200)


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
        params = request.data

        try:
            project_id = params.get('projectId')
            cur_project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        name_project = cur_project.title
        user_id = cur_project.user_id

        argument = {'project': cur_project}

        name_hero = params.get('name')

        if name_hero is None or name_hero == '':
            logger.error('Не указано имя героя')
            raise JsonResponse({'error': 'Не указано имя героя'}, status=500)

        argument['first_name'] = name_hero
        argument['last_name'] = params.get('lastName')
        argument['middle_name'] = params.get('middleName')
        argument['birth_date'] = params.get('dob')
        argument['birth_place'] = params.get('town')

        logger.info('Личные настройки указаны')

        image_data = params.get('image')
        format, imgstr = image_data.split(';base64,')
        ext = format.split('/')[-1]
        argument['photo'] = ContentFile(base64.b64decode(imgstr),
                                        name='{}/{}/{}/{}'.format(user_id,
                                                                  name_project,
                                                                  name_hero,
                                                                  ext))
        logger.info('Изображение загружено')

        obj = Character.objects.create(**argument)

        argument = {'character': obj,
                    'purpose_in_story': params.get('forWhat'),
                    'goal': params.get('goal'),
                    'life_philosophy': params.get('philosophy'),
                    'character_development': params.get('develop')}

        GoalsMotivation.objects.create(**argument)

        argument = {'character': obj,
                    'personal_traits': params.get('personalTraits'),
                    'strengths_weaknesses': params.get('strengthsWeaknesses'),
                    'character_type': params.get('character'),
                    'complexes': params.get('complexes'),
                    'inner_conflicts': params.get('insideConflict'),
                    'individual_style': params.get('style')}

        # личностные особенности
        PersonalityTraits.objects.create(**argument)

        argument = {'character': obj,
                    'biography': params.get('biography'),
                    'relationships_with_others': params.get('relationship')}
        BiographyRelationships.objects.create(**argument)

        argument = {'character': obj,
                    'profession': params.get('profession'),
                    'hobby': params.get('hobby')}
        ProfessionHobbies.objects.create(**argument)

        argument = {'character': obj,
                    'talents': params.get('talents'),
                    'intellectual_abilities': params.get('mindInfo'),
                    'physical_characteristics': params.get('sportInfo'),
                    'external_characteristics': params.get('appearance'),
                    'speech_patterns': params.get('speech')}
        TalentsAbilities.objects.create(**argument)

        logger.info('Второстепенные настройки персонажа зарегестрированы')

        argument['addit_info'] = params.get('additInfo')

        logger.info('Дополнительная информация зарегистрировалась')

        Character.objects.create(**argument)
        logger.info('Персонаж создан')

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse({'message': 'Object created successfully'},
                        status=200)
