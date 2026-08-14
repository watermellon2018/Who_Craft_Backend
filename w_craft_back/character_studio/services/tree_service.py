from __future__ import annotations

import uuid
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from mptt.templatetags.mptt_tags import cache_tree_children

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    VISIBLE_CHARACTER_STATUSES,
    StudioCharacter,
)
from w_craft_back.character_studio.services.errors import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from w_craft_back.character_studio.tree_models import ItemFolder, MenuFolder
from w_craft_back.movie.project import policy
from w_craft_back.movie.project.models import Project


class CharacterTreeService:
    """Manage project-scoped Character Studio tree placements."""

    _CREATE_FIELDS = {
        "id",
        "name",
        "type",
        "parent_id",
        "studio_character_id",
    }
    _UPDATE_FIELDS = {"name"}

    def list_tree(self, principal: UserKey, project_id: int) -> list[dict[str, Any]]:
        project = self._project_for_action(principal, project_id, policy.Action.VIEW)
        nodes = (
            MenuFolder.objects.filter(cur_project=project)
            .select_related("itemfolder__studio_character")
            .order_by("tree_id", "lft")
        )
        roots = cache_tree_children(nodes)
        visible_character_ids = set(
            StudioCharacter.objects.filter(
                project=project,
                status__in=VISIBLE_CHARACTER_STATUSES,
            ).values_list("character_id", flat=True)
        )
        serialized = [
            self._serialize_node(node, project, visible_character_ids)
            for node in roots
        ]
        return [node for node in serialized if node is not None]

    def create_node(
        self,
        principal: UserKey,
        project_id: int,
        data: object,
    ) -> dict[str, Any]:
        payload = self._payload(data, self._CREATE_FIELDS)
        project = self._project_for_action(
            principal,
            project_id,
            policy.Action.EDIT_CONTENT,
        )
        node_id = self._uuid(payload.get("id"), "id")
        name = self._name(payload.get("name"))
        node_type = payload.get("type")
        if node_type not in {"folder", "character"}:
            raise ValidationError("type must be 'folder' or 'character'.")

        parent_supplied = "parent_id" in payload
        parent = self._parent(project, payload.get("parent_id"))
        character = self._studio_character(
            project,
            payload.get("studio_character_id"),
        )
        if node_type == "folder" and character is not None:
            raise ValidationError("Folders cannot link to a studio character.")
        if character is not None and ItemFolder.objects.filter(
            studio_character=character,
        ).exclude(key=node_id).exists():
            raise ValidationError(
                "Studio character already has a tree placement."
            )

        existing = MenuFolder.objects.filter(key=node_id).first()
        if existing is not None:
            if existing.cur_project_id != project.id:
                raise NotFoundError("Tree node not found.")
            return self._complete_placeholder(
                existing,
                node_type=node_type,
                name=name,
                parent=parent,
                parent_supplied=parent_supplied,
                character=character,
            )

        arguments = {
            "key": node_id,
            "name": name,
            "parent": parent,
            "cur_project": project,
        }
        try:
            with transaction.atomic():
                if node_type == "folder":
                    node = MenuFolder.objects.create(is_folder=True, **arguments)
                else:
                    node = ItemFolder.objects.create(
                        is_folder=False,
                        studio_character=character,
                        **arguments,
                    )
        except IntegrityError as exc:
            raise ValidationError("Tree node could not be created.") from exc
        return self._node_payload(node)

    def rename_node(
        self,
        principal: UserKey,
        project_id: int,
        node_id: uuid.UUID,
        data: object,
    ) -> dict[str, Any]:
        payload = self._payload(data, self._UPDATE_FIELDS)
        project = self._project_for_action(
            principal,
            project_id,
            policy.Action.EDIT_CONTENT,
        )
        name = self._name(payload.get("name"))
        with transaction.atomic():
            node = self._node(project, node_id, for_update=True)
            item = self._item(node)
            self._validate_item_project(item, project)
            node.name = name
            node.save(update_fields=["name"])
            if item is not None and item.studio_character_id:
                item.studio_character.name = name
                item.studio_character.save(update_fields=["name", "updated_at"])
        return self._node_payload(node)

    def delete_node(
        self,
        principal: UserKey,
        project_id: int,
        node_id: uuid.UUID,
    ) -> None:
        project = self._project_for_action(
            principal,
            project_id,
            policy.Action.EDIT_CONTENT,
        )
        with transaction.atomic():
            node = self._node(project, node_id, for_update=True)
            descendants = list(
                node.get_descendants(include_self=True).select_related(
                    "cur_project",
                    "itemfolder__studio_character",
                )
            )
            descendant_ids = [child.pk for child in descendants]
            list(
                MenuFolder.objects.select_for_update()
                .filter(pk__in=descendant_ids)
                .values_list("pk", flat=True)
            )
            list(
                ItemFolder.objects.select_for_update()
                .filter(pk__in=descendant_ids)
                .values_list("pk", flat=True)
            )
            if any(child.cur_project_id != project.id for child in descendants):
                raise ValidationError("Tree contains a cross-project placement.")
            for child in descendants:
                self._validate_item_project(self._item(child), project)
            node.delete()

    @staticmethod
    def _payload(data: object, allowed: set[str]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValidationError("A JSON object is required.")
        extra = set(data) - allowed
        if extra:
            raise ValidationError(
                f"Unexpected field(s): {', '.join(sorted(extra))}."
            )
        return data

    @staticmethod
    def _uuid(value: object, field_name: str) -> uuid.UUID:
        try:
            return uuid.UUID(str(value))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValidationError(f"{field_name} must be a valid UUID.") from exc

    @staticmethod
    def _name(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("name is required.")
        name = value.strip()
        if len(name) > 100:
            raise ValidationError("name must contain at most 100 characters.")
        return name

    @staticmethod
    def _project_for_action(
        principal: UserKey,
        project_id: int,
        action: policy.Action,
    ) -> Project:
        try:
            project = (
                Project.objects.filter(policy.accessible_projects_q(principal.user))
                .select_related("owner")
                .distinct()
                .get(id=project_id)
            )
        except (Project.DoesNotExist, TypeError, ValueError) as exc:
            raise NotFoundError("Project not found.") from exc
        if not policy.can(principal.user, project, action):
            raise PermissionDeniedError("Project action is not permitted.")
        return project

    def _parent(self, project: Project, value: object) -> MenuFolder | None:
        if value is None:
            return None
        parent_id = self._uuid(value, "parent_id")
        parent = MenuFolder.objects.filter(
            key=parent_id,
            cur_project=project,
            is_folder=True,
        ).first()
        if parent is None:
            raise NotFoundError("Parent tree folder not found.")
        return parent

    def _studio_character(
        self,
        project: Project,
        value: object,
    ) -> StudioCharacter | None:
        if value is None:
            return None
        character_id = self._uuid(value, "studio_character_id")
        try:
            return StudioCharacter.objects.get(
                project=project,
                character_id=character_id,
            )
        except StudioCharacter.DoesNotExist as exc:
            raise NotFoundError("Studio character not found.") from exc

    @staticmethod
    def _node(
        project: Project,
        node_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> MenuFolder:
        if for_update:
            queryset = MenuFolder.objects.select_for_update()
        else:
            queryset = MenuFolder.objects.select_related(
                "itemfolder__studio_character"
            )
        try:
            return queryset.get(key=node_id, cur_project=project)
        except MenuFolder.DoesNotExist as exc:
            raise NotFoundError("Tree node not found.") from exc

    def _complete_placeholder(
        self,
        node: MenuFolder,
        *,
        node_type: object,
        name: str,
        parent: MenuFolder | None,
        parent_supplied: bool,
        character: StudioCharacter | None,
    ) -> dict[str, Any]:
        if node_type != "character" or node.is_folder:
            raise ValidationError("Tree node already exists.")
        item = self._item(node)
        if item is None:
            raise ValidationError("Existing tree node is not a character placement.")
        if item.studio_character_id is not None:
            expected_parent_id = parent.pk if parent is not None else None
            same_parent = (
                not parent_supplied or item.parent_id == expected_parent_id
            )
            if (
                character is not None
                and item.studio_character_id == character.pk
                and item.name == name
                and same_parent
            ):
                return self._node_payload(item)
            raise ValidationError("Tree node already exists.")
        try:
            with transaction.atomic():
                item.name = name
                item.studio_character = character
                update_fields = ["name", "studio_character"]
                if parent_supplied:
                    item.parent = parent
                    update_fields.append("parent")
                item.save(update_fields=update_fields)
        except IntegrityError as exc:
            raise ValidationError(
                "Studio character already has a tree placement."
            ) from exc
        return self._node_payload(item)

    @staticmethod
    def _item(node: MenuFolder) -> ItemFolder | None:
        try:
            return node.itemfolder
        except ObjectDoesNotExist:
            return None

    @staticmethod
    def _validate_item_project(
        item: ItemFolder | None,
        project: Project,
    ) -> None:
        if (
            item is not None
            and item.studio_character_id is not None
            and item.studio_character.project_id != project.id
        ):
            raise ValidationError("Tree contains a cross-project character link.")

    @classmethod
    def _node_payload(cls, node: MenuFolder) -> dict[str, Any]:
        item = cls._item(node)
        character_id = item.studio_character_id if item is not None else None
        return {
            "id": str(node.key),
            "key": str(character_id or node.key),
            "name": node.name,
            "is_folder": node.is_folder,
            "character_id": str(character_id) if character_id else None,
        }

    @classmethod
    def _serialize_node(
        cls,
        node: MenuFolder,
        project: Project,
        visible_character_ids: set[uuid.UUID],
    ) -> dict[str, Any] | None:
        item = cls._item(node)
        cls._validate_item_project(item, project)
        character_id = item.studio_character_id if item is not None else None
        if not node.is_folder and (
            character_id is None or character_id not in visible_character_ids
        ):
            return None

        response = cls._node_payload(node)
        children = [
            cls._serialize_node(child, project, visible_character_ids)
            for child in node.get_children()
        ]
        visible_children = [child for child in children if child is not None]
        if node.is_folder:
            response["children"] = visible_children
        return response
