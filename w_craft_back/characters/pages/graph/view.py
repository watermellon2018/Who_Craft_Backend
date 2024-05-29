import base64
import logging
import os
import uuid

from django.core.files.base import ContentFile
from django.core.exceptions import ObjectDoesNotExist
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.forms.models import model_to_dict
from django.http import JsonResponse, HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view

from w_craft_back.characters.display_tree.models import ItemFolder
from w_craft_back.characters.pages.graph.model import RelationshipType
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

