import uuid

from django.core.exceptions import (
    ObjectDoesNotExist,
    ValidationError as DjangoValidationError,
)
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from mptt.templatetags.mptt_tags import cache_tree_children
from rest_framework.decorators import api_view
from rest_framework.exceptions import AuthenticationFailed, NotFound, PermissionDenied
from rest_framework.views import APIView

from w_craft_back.auth.models import UserKey
from w_craft_back.auth.utils import resolve_user_key
from w_craft_back.character_studio.models import (
    StudioCharacter,
    VISIBLE_CHARACTER_STATUSES,
)
from w_craft_back.characters.creating.models import Character
from w_craft_back.models import ItemFolder, MenuFolder
from w_craft_back.movie.project import policy
from w_craft_back.movie.project.models import Project


def _looks_like_uuid(value: object) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _looks_like_int(value: object) -> bool:
    if value in (None, ""):
        return False
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _accessible_projects(user):
    return Project.objects.filter(policy.accessible_projects_q(user)).distinct()


def _project_for_action(user, project_id: object, action: policy.Action) -> Project:
    try:
        project = (
            _accessible_projects(user)
            .select_related("owner", "user__user")
            .get(id=project_id)
        )
    except (Project.DoesNotExist, TypeError, ValueError) as exc:
        raise NotFound("Project not found") from exc

    if not policy.can(user, project, action):
        raise PermissionDenied("Project action is not permitted")
    return project


def _node_for_user(user, node_id: object) -> MenuFolder:
    if not _looks_like_uuid(node_id):
        raise NotFound("Tree node not found")

    node = (
        MenuFolder.objects
        .select_related("cur_project__owner", "cur_project__user__user")
        .filter(
            key=node_id,
            cur_project__in=_accessible_projects(user),
        )
        .first()
    )
    if node is None:
        raise NotFound("Tree node not found")
    return node


def _delete_target_for_user(user, target_id: object) -> MenuFolder:
    projects = _accessible_projects(user)
    nodes = (
        MenuFolder.objects
        .select_related("cur_project__owner", "cur_project__user__user")
        .filter(cur_project__in=projects)
    )
    items = (
        ItemFolder.objects
        .select_related("cur_project__owner", "cur_project__user__user")
        .filter(cur_project__in=projects)
    )

    target = None
    if _looks_like_uuid(target_id):
        target = nodes.filter(key=target_id).first()
    if target is None and _looks_like_uuid(target_id):
        target = items.filter(studio_character_id=target_id).first()
    if target is None and _looks_like_int(target_id):
        target = items.filter(hero_id=target_id).first()
    if target is None:
        raise NotFound("Tree node not found")
    return target


def _require_action(user, project: Project | None, action: policy.Action) -> None:
    if project is None:
        raise NotFound("Project not found")
    if not policy.can(user, project, action):
        raise PermissionDenied("Project action is not permitted")


def _invalid_project_link() -> JsonResponse:
    return JsonResponse(
        {"error": "Tree nodes and linked characters must belong to one project"},
        status=400,
    )


def _authenticated_user_key(
    request,
) -> tuple[UserKey | None, JsonResponse | None]:
    try:
        return resolve_user_key(request), None
    except AuthenticationFailed:
        return None, JsonResponse({"detail": "Unauthorized"}, status=401)


@api_view(["POST"])
def rename_character(request):
    user_key, auth_error = _authenticated_user_key(request)
    if auth_error is not None:
        return auth_error
    name = request.data.get("name")
    node_id = request.data.get("id")
    if not isinstance(name, str) or not name.strip():
        return JsonResponse({"error": "Character name is required"}, status=400)

    node = _node_for_user(user_key.user, node_id)
    project = node.cur_project
    _require_action(user_key.user, project, policy.Action.EDIT_CONTENT)

    item = (
        ItemFolder.objects
        .select_related("studio_character", "hero")
        .filter(pk=node.pk)
        .first()
    )
    if (
        item is not None
        and (
            (
                item.studio_character_id
                and item.studio_character.project_id != project.id
            )
            or (item.hero_id and item.hero.project_id != project.id)
        )
    ):
        return _invalid_project_link()

    name = name.strip()
    studio_character_id = None
    with transaction.atomic():
        node.name = name
        node.save(update_fields=["name"])

        if item is not None and item.studio_character_id:
            studio_character_id = str(item.studio_character_id)
            item.studio_character.name = name
            item.studio_character.save(update_fields=["name", "updated_at"])

    return JsonResponse(
        {
            "id": str(node.key),
            "name": name,
            "character_id": studio_character_id,
        },
        status=200,
    )


@api_view(["POST"])
def create_character(request):
    user_key, auth_error = _authenticated_user_key(request)
    if auth_error is not None:
        return auth_error
    project = _project_for_action(
        user_key.user,
        request.data.get("projectId"),
        policy.Action.EDIT_CONTENT,
    )

    name = request.data.get("name")
    node_id = request.data.get("id")
    node_type = request.data.get("type")
    parent_id = request.data.get("parent")
    hero_id = request.data.get("heroID")
    studio_character_id = request.data.get("studioCharacterId")

    if not isinstance(name, str) or not name.strip():
        return JsonResponse({"error": "Character name is required"}, status=400)
    if not _looks_like_uuid(node_id):
        return JsonResponse({"error": "A valid tree node ID is required"}, status=400)
    if node_type not in {"node", "leaf"}:
        return JsonResponse({"error": "Invalid tree node type"}, status=400)

    parent = None
    if parent_id is not None:
        if not _looks_like_uuid(parent_id):
            raise NotFound("Parent tree node not found")
        parent = MenuFolder.objects.filter(
            key=parent_id,
            cur_project=project,
        ).first()
        if parent is None:
            raise NotFound("Parent tree node not found")

    existing_node = (
        MenuFolder.objects
        .select_related("cur_project")
        .filter(key=node_id, cur_project=project)
        .first()
    )

    arguments = {
        "name": name.strip(),
        "key": str(node_id),
        "user": user_key,
        "cur_project": project,
    }
    if parent is not None:
        arguments["parent"] = parent

    if node_type == "node":
        if existing_node is not None:
            return JsonResponse({"error": "Tree node already exists"}, status=400)
        try:
            with transaction.atomic():
                MenuFolder.objects.create(is_folder=True, **arguments)
        except IntegrityError as exc:
            raise NotFound("Tree node not found") from exc
        return HttpResponse(status=200)

    studio_character = None
    if studio_character_id:
        try:
            studio_character = StudioCharacter.objects.get(
                project=project,
                character_id=studio_character_id,
            )
        except (StudioCharacter.DoesNotExist, DjangoValidationError) as exc:
            raise NotFound("Studio character not found") from exc

    hero = None
    if hero_id:
        try:
            hero = Character.objects.get(project=project, id=hero_id)
        except (Character.DoesNotExist, TypeError, ValueError) as exc:
            raise NotFound("Legacy character not found") from exc

    if existing_node is not None:
        if existing_node.is_folder:
            return JsonResponse({"error": "Existing tree node is a folder"}, status=400)

        item = (
            ItemFolder.objects
            .select_related("studio_character", "hero")
            .filter(pk=existing_node.pk, cur_project=project)
            .first()
        )
        if item is None:
            return JsonResponse(
                {"error": "Existing tree node cannot be linked to a character"},
                status=400,
            )
        if (
            item.studio_character_id
            and item.studio_character.project_id != project.id
        ) or (item.hero_id and item.hero.project_id != project.id):
            return _invalid_project_link()

        item.name = name.strip()
        if studio_character is not None:
            item.studio_character = studio_character
        if hero is not None:
            item.hero = hero
        item.save()
        return HttpResponse(status=200)

    if studio_character is not None:
        arguments["studio_character"] = studio_character
    if hero is not None:
        arguments["hero"] = hero
    try:
        with transaction.atomic():
            ItemFolder.objects.create(is_folder=False, **arguments)
    except IntegrityError as exc:
        raise NotFound("Tree node not found") from exc
    return HttpResponse(status=200)


class CharacterTreeDelete(APIView):
    def post(self, request):
        user_key, auth_error = _authenticated_user_key(request)
        if auth_error is not None:
            return auth_error
        target = _delete_target_for_user(user_key.user, request.data.get("id"))
        project = target.cur_project
        _require_action(user_key.user, project, policy.Action.EDIT_CONTENT)

        with transaction.atomic():
            target = (
                MenuFolder.objects
                .select_for_update()
                .filter(pk=target.pk, cur_project=project)
                .first()
            )
            if target is None:
                raise NotFound("Tree node not found")

            descendants = list(
                target.get_descendants(include_self=True)
                .select_related("cur_project")
            )
            if any(node.cur_project_id != project.id for node in descendants):
                return _invalid_project_link()

            node_ids = [node.pk for node in descendants]
            list(
                MenuFolder.objects
                .select_for_update()
                .filter(pk__in=node_ids)
                .values_list("pk", flat=True)
            )
            list(
                ItemFolder.objects
                .select_for_update()
                .filter(pk__in=node_ids)
                .values_list("pk", flat=True)
            )
            items = list(
                ItemFolder.objects
                .filter(pk__in=node_ids)
                .select_related("studio_character", "hero")
            )
            for item in items:
                if (
                    item.studio_character_id
                    and item.studio_character.project_id != project.id
                ) or (item.hero_id and item.hero.project_id != project.id):
                    return _invalid_project_link()

            studio_character_ids = [
                item.studio_character_id
                for item in items
                if item.studio_character_id is not None
            ]
            if studio_character_ids:
                backlink_project_ids = list(
                    ItemFolder.objects
                    .select_for_update()
                    .filter(studio_character_id__in=studio_character_ids)
                    .values_list("cur_project_id", flat=True)
                )
                if any(
                    linked_project_id != project.id
                    for linked_project_id in backlink_project_ids
                ):
                    return _invalid_project_link()

            target.delete()
            if studio_character_ids:
                StudioCharacter.objects.filter(
                    project=project,
                    character_id__in=studio_character_ids,
                ).delete()

        return JsonResponse({"message": "Object deleted successfully"}, status=200)


class CharacterTree(APIView):
    def get(self, request):
        user_key, auth_error = _authenticated_user_key(request)
        if auth_error is not None:
            return auth_error
        project = _project_for_action(
            user_key.user,
            request.query_params.get("projectId"),
            policy.Action.VIEW,
        )

        items = (
            MenuFolder.objects
            .filter(cur_project=project)
            .select_related(
                "itemfolder__studio_character",
                "itemfolder__hero",
            )
            .order_by("tree_id", "lft")
        )
        tree = cache_tree_children(items)

        visible_studio_character_ids = set(
            StudioCharacter.objects
            .filter(project=project, status__in=VISIBLE_CHARACTER_STATUSES)
            .values_list("character_id", flat=True)
        )

        def build_tree(node):
            studio_character_id = None
            legacy_hero_id = None
            try:
                item = node.itemfolder
                if (
                    item.studio_character_id
                    and item.studio_character.project_id != project.id
                ) or (item.hero_id and item.hero.project_id != project.id):
                    return None
                studio_character_id = item.studio_character_id
                legacy_hero_id = item.hero_id
            except ObjectDoesNotExist:
                pass

            response = {
                "id": str(node.key),
                "key": str(studio_character_id or legacy_hero_id or node.key),
                "name": node.name,
                "is_folder": node.is_folder,
                "character_id": (
                    str(studio_character_id) if studio_character_id else None
                ),
                "legacy_hero_id": legacy_hero_id,
            }
            raw_children = [build_tree(child) for child in node.get_children()]
            children = [child for child in raw_children if child is not None]

            if not node.is_folder:
                if not studio_character_id and not legacy_hero_id:
                    return None
                if (
                    studio_character_id
                    and studio_character_id not in visible_studio_character_ids
                ):
                    return None
                if not children:
                    return response

            response["children"] = children
            return response

        tree_json = [build_tree(node) for node in tree]
        tree_json = [node for node in tree_json if node is not None]
        return JsonResponse(tree_json, safe=False, status=200)
