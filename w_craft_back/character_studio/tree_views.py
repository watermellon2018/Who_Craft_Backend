from __future__ import annotations

import logging
import uuid
from functools import wraps

from django.http import HttpResponse, JsonResponse
from rest_framework.decorators import api_view
from rest_framework.exceptions import APIException

from w_craft_back.character_studio.services.errors import CharacterStudioError
from w_craft_back.character_studio.services.permissions import get_user_from_request
from w_craft_back.character_studio.services.tree_service import CharacterTreeService


logger = logging.getLogger(__name__)


def _tree_errors(func):
    @wraps(func)
    def wrapped(request, *args, **kwargs):
        try:
            return func(request, *args, **kwargs)
        except CharacterStudioError as exc:
            return JsonResponse(
                {"error_code": exc.error_code, "message": exc.message},
                status=exc.status_code,
            )
        except APIException:
            raise
        except Exception:
            logger.exception("Unhandled character tree error in %s", func.__name__)
            return JsonResponse(
                {"error_code": "INTERNAL_ERROR", "message": "internal_error"},
                status=500,
            )

    return wrapped


@api_view(["GET"])
@_tree_errors
def character_tree(request, project_id: int):
    principal = get_user_from_request(request)
    tree = CharacterTreeService().list_tree(principal, project_id)
    return JsonResponse(tree, safe=False, status=200)


@api_view(["POST"])
@_tree_errors
def character_tree_nodes(request, project_id: int):
    principal = get_user_from_request(request)
    node = CharacterTreeService().create_node(
        principal,
        project_id,
        request.data,
    )
    return JsonResponse(node, status=201)


@api_view(["PATCH", "DELETE"])
@_tree_errors
def character_tree_node_detail(
    request,
    project_id: int,
    node_id: uuid.UUID,
):
    principal = get_user_from_request(request)
    service = CharacterTreeService()
    if request.method == "PATCH":
        node = service.rename_node(principal, project_id, node_id, request.data)
        return JsonResponse(node, status=200)
    service.delete_node(principal, project_id, node_id)
    return HttpResponse(status=204)
