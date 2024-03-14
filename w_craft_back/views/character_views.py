from w_craft_back.auth.models import UserKey
from w_craft_back.models import MenuFolder, ItemFolder

import logging

from django.http import JsonResponse, HttpResponse
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

    user_token = request.data['token_user']
    cur_user = UserKey.objects.get(key=user_token)
    logger.info(f'Пользователь {cur_user.key}')

    project_name = request.data['projectTitle']
    logger.info(project_name)
    cur_project = Project.objects.get(title=project_name)
    logger.info(f'Персонаж из проекта {cur_project.title}')


    name = request.data['name']
    id = request.data['id']
    id = str(id)
    parent_id = request.data['parent']

    arguments = {'name': name,
                 'key': id,
                 'user': cur_user,
                 'cur_project': cur_project}

    if parent_id is None:
        pass
    else:
        parent_obj = MenuFolder.objects.get(key=parent_id)
        logger.info(parent_obj)
        arguments['parent'] = parent_obj

    if request.data['type'] == 'node':
        arguments['is_folder'] = True
        MenuFolder.objects.create(**arguments)
        logger.info('Папка в дереве персонажей создана')
    elif request.data['type'] == 'leaf':
        arguments['is_folder'] = False
        ItemFolder.objects.create(**arguments)
        logger.info('Лист (персонаж) создан')
    else:
        logger.error('Неправильный тип элемента в дереве персонажей')
        return JsonResponse({'message': 'Not correct element in tree of characters'}, status=500)

    return HttpResponse(status=200)


class CharacterTree(APIView):

    def post(self, request):
        try:
            logger.info('Удаление персонажа из дерева')
            id_to_delete = request.data.get('id')
            model_to_delete = MenuFolder.objects.get(key=id_to_delete)
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
            cur_project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            logger.error('Проект не найден')
            return JsonResponse(
                {'error': 'Object with specified ID does not exist'},
                status=404)

        items = MenuFolder.objects.filter(cur_project=cur_project)

        tree = cache_tree_children(items)

        # Преобразуем дерево в формат JSON
        def build_tree(node):
            response = {
                'id': str(node.key),
                'key': node.name+'_'+str(node.id),
                'name': node.name,
            }
            children = [build_tree(child) for child in node.get_children()]


            if len(children) == 0 and not node.is_folder:
                return response
            response['children'] = children
            return response

        tree_json = [build_tree(node) for node in tree]

        return JsonResponse(tree_json, safe=False, status=200)
