from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    StudioCharacter,
    VISIBLE_CHARACTER_STATUSES,
)
from w_craft_back.characters.creating.models import Character
from w_craft_back.models import MenuFolder, ItemFolder

import logging
import uuid

from django.core.exceptions import ObjectDoesNotExist
from django.http import JsonResponse, HttpResponse
from mptt.templatetags.mptt_tags import cache_tree_children
from rest_framework.decorators import api_view
from rest_framework.views import APIView

from w_craft_back.movie.project.models import Project

logger = logging.getLogger(__name__)


def _looks_like_uuid(value):
    if not value:
        return False
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _looks_like_int(value):
    if value in (None, ""):
        return False
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


@api_view(['POST'])
def rename_character(request):
    try:
        logger.info('Изменение имени персонажа')
        name = request.data['name']
        id = request.data['id']
        obj = MenuFolder.objects.get(key=id)
        obj.name = name
        obj.save()

        studio_character_id = None
        try:
            item = obj.itemfolder
            if item.studio_character_id:
                studio_character_id = str(item.studio_character_id)
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

    return JsonResponse({"id": id, "name": name, "character_id": studio_character_id}, status=200)


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

            # Three lookup keys are tried in order: MenuFolder.key and
            # ItemFolder.studio_character_id are UUIDs; ItemFolder.hero_id is
            # a legacy integer FK. Mixing types crashes the ORM (e.g. passing
            # a UUID into an int FK lookup), so we filter to compatible
            # candidates before running each query.
            model_to_delete = None
            if id_to_delete:
                model_to_delete = MenuFolder.objects.filter(key=id_to_delete).first()
            if model_to_delete is None and _looks_like_uuid(id_to_delete):
                model_to_delete = ItemFolder.objects.filter(
                    studio_character_id=id_to_delete,
                ).first()
            if model_to_delete is None and _looks_like_int(id_to_delete):
                model_to_delete = ItemFolder.objects.filter(hero_id=id_to_delete).first()
            if model_to_delete is None:
                return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)

            for node in model_to_delete.get_descendants(include_self=True):
                try:
                    studio_character = node.itemfolder.studio_character
                    if studio_character:
                        studio_character.delete()
                except ObjectDoesNotExist:
                    pass
            model_to_delete.delete()

            return JsonResponse({'message': 'Object deleted successfully'}, status=200)

        except MenuFolder.DoesNotExist:
            return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)

        except Exception as e:
            # The previous handler swallowed the traceback, which made
            # "delete returns 500" bugs invisible in logs. Keep the same
            # response shape but actually log the exception.
            logger.exception('Tree delete failed for id=%s', request.data.get('id'))
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

        # Drafts must NEVER appear in the tree — they're unfinished
        # creation attempts and showing them produced duplicate "name"
        # entries next to the user's real character.
        visible_studio_character_ids = set(
            StudioCharacter.objects
            .filter(project=cur_project, status__in=VISIBLE_CHARACTER_STATUSES)
            .values_list('character_id', flat=True)
        )

        # Преобразуем дерево в формат JSON
        def build_tree(node):
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
            raw_children = [build_tree(child) for child in node.get_children()]
            children = [child for child in raw_children if child is not None]

            if not node.is_folder:
                # Dangling leaf: tree node was created but the user never
                # finished and didn't link it to anything. Hide it.
                if not studio_character_id and not legacy_hero_id:
                    return None
                # Linked to a studio character that's still a draft (the user
                # bailed before applying a variant). Hide it too.
                if studio_character_id and studio_character_id not in visible_studio_character_ids:
                    return None
                if not children:
                    return response

            response['children'] = children
            return response

        tree_json = [build_tree(node) for node in tree]
        tree_json = [node for node in tree_json if node is not None]
        logger.info('Дерево персонажей получено')

        return JsonResponse(tree_json, safe=False, status=200)
