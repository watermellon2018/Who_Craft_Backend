"""Dependency-free CI guard for the checked-in OpenAPI contract."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "openapi.json"


def _schema(document: dict[str, Any], name: str) -> dict[str, Any]:
    return document["components"]["schemas"][name]


def _integer_expression(node: ast.expr) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _integer_expression(node.left) * _integer_expression(node.right)
    raise AssertionError(
        "Contract constants must be integer literals or multiplications"
    )


def _integer_constant(relative_path: str, name: str) -> int:
    module_path = ROOT / relative_path
    module = ast.parse(
        module_path.read_text(encoding="utf-8"),
        filename=str(module_path),
    )
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        ):
            return _integer_expression(statement.value)
    raise AssertionError(f"Missing backend contract constant: {relative_path}:{name}")


def check_contract() -> None:
    with SCHEMA_PATH.open("r", encoding="utf-8") as schema_file:
        document = json.load(schema_file)

    assert document["openapi"] == "3.0.3"
    security = document["components"]["securitySchemes"]["userToken"]
    assert security == {"type": "apiKey", "in": "header", "name": "X-User-Token"}

    constraints = document["x-contract-constraints"]
    assert constraints["posterPromptMaxLength"] == _integer_constant(
        "w_craft_back/movie/poster/serializers.py", "PROMPT_MAX_LENGTH"
    )
    assert constraints["projectAnnotationMaxLength"] == _integer_constant(
        "w_craft_back/movie/project/serializers.py", "PROJECT_ANNOTATION_MAX_LENGTH"
    )
    assert constraints["projectSynopsisMaxLength"] == _integer_constant(
        "w_craft_back/movie/project/serializers.py", "PROJECT_SYNOPSIS_MAX_LENGTH"
    )
    assert constraints["projectPosterMaxBytes"] == _integer_constant(
        "w_craft_back/movie/project/project_images.py", "MAX_PROJECT_IMAGE_BYTES"
    )

    tree_create = _schema(document, "CharacterTreeCreateRequest")
    assert "token_user" not in tree_create["properties"]
    assert {"id", "name", "type"}.issubset(tree_create["required"])
    assert "projectId" not in tree_create["properties"]
    assert set(tree_create["properties"]["type"]["enum"]) == {
        "folder",
        "character",
    }
    tree_node = _schema(document, "CharacterTreeNode")
    assert "legacy_hero_id" not in tree_node["properties"]

    poster_prompt = _schema(document, "PosterGenerateRequest")["properties"]["prompt"]
    project = _schema(document, "ProjectMutationRequest")["properties"]
    assert poster_prompt["maxLength"] == constraints["posterPromptMaxLength"]
    assert (
        project["annotation"]["maxLength"]
        == constraints["projectAnnotationMaxLength"]
    )
    assert project["synopsis"]["maxLength"] == constraints["projectSynopsisMaxLength"]

    error = _schema(document, "ApiErrorEnvelope")
    assert {"error", "code", "detail"}.issubset(error["properties"])

    required_paths = {
        "/api/projects/{projectId}/character-tree/",
        "/api/projects/{projectId}/character-tree/nodes/",
        "/api/projects/{projectId}/character-tree/nodes/{nodeId}/",
        "/api/projects/{projectId}/poster/generate/",
        "/api/projects/{projectId}/team/invitations/",
        "/api/credits/project-budgets/",
        "/api/credits/project-budgets/{projectId}/",
    }
    assert required_paths.issubset(document["paths"])
    assert not any(path.startswith("/api/character/") for path in document["paths"])
    assert (
        document["paths"]["/api/projects/{projectId}/"]["get"]["operationId"]
        == "getProject"
    )
    admin_operation = _schema(document, "CreditAdminOperationRequest")
    assert set(admin_operation["properties"]) == {"action", "reason"}
    assert set(admin_operation["properties"]["action"]["enum"]) == {
        "freeze",
        "unfreeze",
    }
    transfer = _schema(document, "CreditTransferRequest")
    assert set(transfer["required"]) == {
        "senderUsername",
        "recipientUsername",
        "amount",
        "reason",
    }


if __name__ == "__main__":
    check_contract()
    print("OpenAPI contract checks passed")
