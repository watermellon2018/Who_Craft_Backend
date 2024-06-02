import base64
import logging
import os
import uuid
from w_craft_back.auth.models import UserKey
from django.core.files.base import ContentFile
from django.core.exceptions import ObjectDoesNotExist
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.forms.models import model_to_dict
from django.http import JsonResponse, HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view

from w_craft_back.characters.display_tree.models import ItemFolder
from w_craft_back.characters.pages.graph.model import RelationshipType, GraphEdge
from w_craft_back.movie.project.models import Project
from w_craft_back.characters.creating.models import Character, \
    GoalsMotivation, BiographyRelationships, PersonalityTraits, \
    ProfessionHobbies, TalentsAbilities

logger = logging.getLogger(__name__)


@api_view(['GET'])
def select_all_type_relationship(request):
    logger.info('Возвращаем все возможные взаимоотношения между персонажами')

    items = RelationshipType.objects.all()

    def build_json(node):
        response = {
            'key': 'type_between_heroes_' + node.name,
            'value': node.translit,
            'name': node.name,
        }

        return response

    genres_json = [build_json(node) for node in items]
    return JsonResponse(genres_json, safe=False)


@api_view(['GET'])
def select_edges(request):
    logger.info('Возвращаем все ребра графа взаимоотношений персонажей')

    user_token = request.GET.get('token_user')
    cur_user = UserKey.objects.get(key=user_token)

    try:
        logger.info('Select запрос проекта')
        id = request.GET.get('projectId')
        project = Project.objects.get(id=id, user=cur_user)
        logger.info(f'Проект найден id: {id}')

        edges = GraphEdge.objects.filter(project=project, user=cur_user)

        def build_json(edge):
            response = {
                'id': f'{edge.from_node}_{edge.to_node}',
                'from': edge.from_node,
                'to': edge.to_node,
                'label': edge.label.name,
            }
            logger.info(response)
            return response

        genres_json = [build_json(edge) for edge in edges]

        return JsonResponse(genres_json, safe=False)

    except Project.DoesNotExist:
        logger.error('Object with specified ID does not exist')
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=500)

    except Exception as e:
        logger.error(str(e))
        return JsonResponse({'error': str(e)}, status=500)



@api_view(['POST'])
def add_edges(request):
    logger.info('Создаем ребро в графе взаимоотношений персонажей')

    data = request.data['data']
    user_token = data['token_user']
    cur_user = UserKey.objects.get(key=user_token)

    try:
        logger.info('Select запрос проекта')
        id = data['projectId']
        project = Project.objects.get(id=id, user=cur_user)
        logger.info(f'Проект найден id: {id}')

        label = data['label']
        relationship = RelationshipType.objects.filter(translit=label)[0]

        logger.info('Тип отношений {}'.format(relationship.name))

        arguments = {
            'user': cur_user,
            'project': project,
            'to_node': data['to'],
            'from_node': data['from'],
            'label': relationship,
        }
        GraphEdge.objects.create(**arguments)
        logger.info('Ребро в графе персонажей создано')

        return HttpResponse(status=status.HTTP_200_OK)

    except Project.DoesNotExist:
        logger.error('Object with specified ID does not exist')
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=500)

    except Exception as e:
        logger.error(str(e))
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['GET'])
def delete_edge(request):
    logger.info('Удаляем ребро в графе взаимоотношений персонажей')
    user_token = request.GET.get('token_user')
    cur_user = UserKey.objects.get(key=user_token)

    try:
        logger.info('Select запрос проекта')
        id = request.GET.get('id')
        project = Project.objects.get(id=id, user=cur_user)
        logger.info(f'Проект найден id: {id}')

        from_node = request.GET.get('from_node')
        to_node = request.GET.get('to_node')

        label = request.GET.get('label')
        relationship = RelationshipType.objects.filter(name=label)[0]
        logger.info('Тип отношений {}'.format(relationship.name))

        obj = GraphEdge.objects.get(user=cur_user,
                                    project=project,
                                    from_node=from_node,
                                    to_node=to_node,
                                    label=relationship)
        obj.delete()
        logger.info('Ребро в графе взаимоотношений персонажей удалено')

        return HttpResponse(status=status.HTTP_200_OK)

    except Project.DoesNotExist:
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@api_view(['POST'])
def update_info_edge(request):
    logger.info('Обновить информацию о ребре в графе взаимоотношений')
    data = request.data['data']
    user_token = data['token_user']
    cur_user = UserKey.objects.get(key=user_token)

    try:
        id = data['id']
        project = Project.objects.get(id=id, user=cur_user)
        logger.info(f'Проект найден id: {id}')
        from_node = request.GET.get('from_node')
        to_node = request.GET.get('to_node')

        obj = GraphEdge.objects.get(user=cur_user,
                              project=project,
                              from_node=from_node,
                              to_node=to_node)
        logger.info('Объект найден {}'.format(obj.label))

        label = request.GET.get('label')
        relationship = RelationshipType.objects.filter(name=label)[0]
        logger.info('Тип отношений {}'.format(relationship.name))

        obj.label.set(relationship)
        obj.save()

        return HttpResponse(status=status.HTTP_200_OK)

    except Project.DoesNotExist:
        return JsonResponse({'error': 'Object with specified ID does not exist'}, status=404)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)