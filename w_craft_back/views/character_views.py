from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import CharacterStatus, StudioCharacter
from w_craft_back.characters.creating.models import Character
from w_craft_back.models import MenuFolder, ItemFolder

import logging

from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from mptt.templatetags.mptt_tags import cache_tree_children
from rest_framework.decorators import api_view
from rest_framework.views import APIView

from w_craft_back.movie.project.models import Project

logger = logging.getLogger(__name__)


@api_view(['POST'])
def rename_character(request):
    try:
        logger.info('Изменение имени персонажа')
        name = request.data['name']
        id = request.data['id']
        obj = MenuFolder.objects.get(key=id)
        obj.name = name
        obj.save()

        try:
            item = obj.itemfolder
            if item.studio_character_id:
                item.studio_character.name = name
                item.studio_character.save(update_fields=["name", "updated_at"])
        except ObjectDoesNotExist:
            pass

        logger.info('Имя изменено')
    except MenuFolder.DoesNotExist:
        return JsonResponse({'error': 'Object with specified ID does not exist'},
                            status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    return HttpResponse(status=200)


@api_view(['POST'])
def create_character(request):
    logger.info(request.data)
    logger.info('Создание персонажа')
    hero_id = request.data.get('heroID')
    studio_character_id = request.data.get('studioCharacterId')

    user_token = request.data['token_user']
    cur_user = UserKey.objects.get(key=user_token)
    logger.info(f'Пользователь {cur_user.key}')

    try:
        project_id = request.data['projectId']
        cur_project = Project.objects.get(id=project_id)
    except Project.DoesNotExist:
        logger.error('Проект не найден')
        return JsonResponse(
            {'error': 'Project with specified ID does not exist'},
            status=404)

    name = request.data['name']
    id = request.data['id']
    id = str(id)
    parent_id = request.data['parent']

    arguments = {'name': name,
                 'key': id,
                 'user': cur_user,
                 'cur_project': cur_project}

    if parent_id is None:
        logger.info('Родитель не указан')
        pass
    else:
        parent_obj = MenuFolder.objects.get(key=parent_id)
        logger.info('Родитель найден')
        arguments['parent'] = parent_obj

    if request.data['type'] == 'node':
        arguments['is_folder'] = True
        MenuFolder.objects.create(**arguments)
        logger.info('Папка в дереве персонажей создана')
    elif request.data['type'] == 'leaf':
        arguments['is_folder'] = False
        existing_node = MenuFolder.objects.filter(key=id).first()

        if studio_character_id:
            try:
                studio_character = StudioCharacter.objects.get(
                    project=cur_project,
                    user=cur_user,
                    character_id=studio_character_id,
                )
            except StudioCharacter.DoesNotExist:
                logger.error('Персонаж Character Studio не найден')
                return JsonResponse(
                    {'error': 'Studio character with specified ID does not exist'},
                    status=404)
            arguments['studio_character'] = studio_character
        elif hero_id:
            try:
                hero = Character.objects.get(project=cur_project, id=hero_id)
                logger.info(hero)
            except Character.DoesNotExist:
                logger.error('Герой не найден')
                return JsonResponse(
                    {'error': 'Hero with specified ID does not exist'},
                    status=404)
            arguments['hero'] = hero

        if existing_node:
            if existing_node.is_folder:
                return JsonResponse(
                    {'error': 'Existing tree node is a folder'},
                    status=400)

            try:
                item = existing_node.itemfolder
            except ObjectDoesNotExist:
                return JsonResponse(
                    {'error': 'Existing tree node cannot be linked to a character'},
                    status=400)

            item.name = name
            if studio_character_id:
                item.studio_character = studio_character
            if hero_id:
                item.hero = hero
            item.save()
            logger.info('Лист (персонаж) обновлен')
            return HttpResponse(status=200)

        logger.info(arguments)
        ItemFolder.objects.create(**arguments)
        logger.info('Лист (персонаж) создан')
    else:
        logger.error('Неправильный тип элемента в дереве персонажей')
        return JsonResponse({'message': 'Not correct element in tree of characters'}, status=500)

    logger.info('Все прошло успешно!')
    return HttpResponse(status=200)


class CharacterTree(APIView):

    def post(self, request):
        try:
            logger.info('Удаление персонажа из дерева')
            id_to_delete = request.data.get('id')
            model_to_delete = MenuFolder.objects.get(key=id_to_delete)
            for node in model_to_delete.get_descendants(include_self=True):
                try:
                    studio_character = node.itemfolder.studio_character
                    if studio_character:
                        studio_character.status = CharacterStatus.ARCHIVED
                        studio_character.archived_at = timezone.now()
                        studio_character.save(update_fields=["status", "archived_at", "updated_at"])
                except ObjectDoesNotExist:
                    pass
            model_to_delete.delete()

            return JsonResponse({'message': 'Object deleted successfully'}, status=200)

        except MenuFolder.DoesNotExist:
            return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    def get(self, request):
        logger.info('Получаем список персонажей для отображение на дереве')
        try:
            project_id = request.GET.get('projectId')
            logger.info(f'Проект номер {project_id}')
            cur_project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            logger.error('Проект не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        items = MenuFolder.objects.filter(cur_project=cur_project).order_by('tree_id', 'lft')
        tree = cache_tree_children(items)

        # Преобразуем дерево в формат JSON
        def build_tree(node):
            item = None
            studio_character_id = None
            legacy_hero_id = None
            try:
                item = node.itemfolder
                studio_character_id = item.studio_character_id
                legacy_hero_id = item.hero_id
            except ObjectDoesNotExist:
                pass

            response = {
                'id': str(node.key),
                'key': str(studio_character_id or legacy_hero_id or node.key),
                'name': node.name,
                'is_folder': node.is_folder,
                'character_id': str(studio_character_id) if studio_character_id else None,
                'legacy_hero_id': legacy_hero_id,
            }
            children = [build_tree(child) for child in node.get_children()]

            if len(children) == 0 and not node.is_folder:
                return response
            response['children'] = children
            return response

        tree_json = [build_tree(node) for node in tree]
        logger.info('Дерево персонажей получено')

        return JsonResponse(tree_json, safe=False, status=200)
