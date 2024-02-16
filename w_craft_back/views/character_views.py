from w_craft_back.models import MenuFolder

import logging

from django.http import JsonResponse
from mptt.templatetags.mptt_tags import cache_tree_children
from django.http import HttpResponse
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class CharacterTree(APIView):
    def get(self, request):
        logger.info('Request to get to character list')
        # params = request.GET

        # project = params.get('project') # TODO::

        items = MenuFolder.objects.all()
        # logger.info(items)

        tree = cache_tree_children(items)
        logger.info(tree)

        # Преобразуем дерево в формат JSON
        def build_tree(node):
            return {
                'id': node.id,
                'key': node.name,
                'name': node.name,
                'children': [build_tree(child) for child in node.get_children()]
            }

        tree_json = [build_tree(node) for node in tree]
        logger.info(tree_json)

        return JsonResponse(tree_json, safe=False)
        # logger.info(dir(items))

        # items_dict = model_to_dict(items, fields=[field.name for field in items._meta.fields])
        # response = HttpResponse(items)

        # return response
