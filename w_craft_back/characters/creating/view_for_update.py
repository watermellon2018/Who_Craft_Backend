import logging

from django.http import JsonResponse, HttpResponse
from rest_framework.decorators import api_view

from w_craft_back.characters.creating.utils import check_exist_project
from w_craft_back.movie.project.models import Project
from w_craft_back.characters.creating.models import Character, \
    GoalsMotivation, BiographyRelationships, PersonalityTraits, \
    ProfessionHobbies, TalentsAbilities

logger = logging.getLogger(__name__)


@api_view(['POST'])
def update_personal_info(request):
    try:
        logger.info('Обновляем личную информацию о герое')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        name_hero = params['name']
        if name_hero is None or name_hero == '':
            logger.error('Не указано имя героя')
            raise JsonResponse({'error': 'Не указано имя героя'}, status=500)
        logger.info('Имя героя корректно')

        hero.first_name = name_hero
        hero.last_name = params['lastName']
        hero.middle_name = params['middleName']
        hero.birth_date = params['dob']
        hero.birth_place = params['town']
        hero.save()

        logger.info('Личные настройки обновлены')

        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
def update_motivate_info(request):
    try:
        logger.info('Обновляем мотивацию героя')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        data = GoalsMotivation.objects.get(character=hero)
        data.purpose_in_story = params['forWhat']
        data.goal = params['goal']
        data.life_philosophy = params['philosophy']
        data.save()

        logger.info('Мотивация обновлена')

        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
def update_inside_data_hero(request):
    try:
        logger.info('Обновляем внутренние качества героя')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        data = PersonalityTraits.objects.get(character=hero)
        data.personal_traits = params['personalTraits']
        data.strengths_weaknesses = params['strengthsWeaknesses']
        data.character_type = params['character']
        data.save()

        logger.info('Внутренняя информация обновлена')
        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
def update_competition_data_hero(request):
    try:
        logger.info('Обновляем компетенции героя')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        data = ProfessionHobbies.objects.get(character=hero)
        data.profession = params['profession']
        data.hobbies = params['hobby']
        data.save()

        data = TalentsAbilities.objects.get(character=hero)
        data.talents = params['talents']
        data.intellectual_abilities = params['mindInfo']
        data.physical_characteristics = params['sportInfo']
        data.save()

        logger.info('Компетенции персонажа обновлены')
        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
def update_identity_data_hero(request):
    try:
        logger.info('Обновляем характеристики идентифицирующие героя героя')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        data = PersonalityTraits.objects.get(character=hero)
        data.individual_style = params['style']
        data.complexes = params['complexs']
        data.external_characteristics = params['appearance']
        data.save()

        data = TalentsAbilities.objects.get(character=hero)
        data.speech_patterns = params['speech']
        data.save()


        logger.info('Характеристики идентифицирующие персонажа обновлены')
        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
def update_psyho_data_hero(request):
    try:
        logger.info('Обновляем поле внутренние конфликты персонажа')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        data = PersonalityTraits.objects.get(character=hero)
        data.inner_conflicts = params['insideConflict']
        data.save()

        logger.info('Внутренние конфилкты героя обновлены')
        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
def update_development_data_hero(request):
    try:
        logger.info('Обновляем поле развитие персонажа')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        data = GoalsMotivation.objects.get(character=hero)
        data.character_development = params['development']
        data.save()

        logger.info('Поле развитие персонажа обновлено')
        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
def update_addit_data_hero(request):
    try:
        logger.info('Обновляем поле дополнительная информация о персонаже')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        data = BiographyRelationships.objects.get(character=hero)
        data.addit_info = params['additInfo']
        data.save()

        logger.info('Поле дополнительная информация о персонаже обновлено')
        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
def update_bio_data_hero(request):
    try:
        logger.info('Обновляем поле биография персонажа')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        data = BiographyRelationships.objects.get(character=hero)
        data.biography = params['bio']
        data.save()

        logger.info('Поле биография персонажа обновлено')
        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_view(['POST'])
def update_relationship_data_hero(request):
    try:
        logger.info('Обновляем поле отношения персонажа с другими героями')
        params = request.data['data']
        logger.info(params)

        try:
            cur_project = check_exist_project(params['projectId'])
        except Project.DoesNotExist:
            logger.error('Проект для которого создается персонаж, не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        hero_id = params['characterId']
        hero = Character.objects.get(id=hero_id, project=cur_project)
        logger.info('Герой для которого нужно обновить информацию найден')

        data = BiographyRelationships.objects.get(character=hero)
        data.relationships_with_others = params['relationship']
        data.save()

        logger.info('Поле обновлено')
        return HttpResponse(status=200)


    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)