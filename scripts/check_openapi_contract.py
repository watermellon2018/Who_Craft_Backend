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
        "/api/auth/logout-all/",
        "/api/profile/settings/",
        "/api/notifications/",
        "/api/notifications/{notificationId}/read/",
        "/api/notifications/read-all/",
        "/api/projects/{projectId}/video-shots/{shotId}/comments/",
        "/api/projects/{projectId}/character-tree/",
        "/api/projects/{projectId}/character-tree/nodes/",
        "/api/projects/{projectId}/character-tree/nodes/{nodeId}/",
        "/api/projects/{projectId}/scenes/missing-characters/",
        "/api/projects/{projectId}/video/preparation/",
        "/api/projects/{projectId}/poster/generate/",
        "/api/projects/{projectId}/team/invitations/",
        "/api/projects/{projectId}/storyboard/scenes/",
        "/api/projects/{projectId}/storyboard/scenes/{sceneId}/",
        "/api/projects/{projectId}/storyboard/{storyboardId}/shots/",
        "/api/projects/{projectId}/storyboard/keyframes/{keyframeId}/generate/",
        "/api/projects/{projectId}/storyboard/keyframes/{keyframeId}/regenerate/",
        "/api/projects/{projectId}/storyboard/generations/{generationId}/",
        "/api/credits/project-budgets/",
        "/api/credits/project-budgets/{projectId}/",
    }
    assert required_paths.issubset(document["paths"])
    profile_settings = _schema(document, "ProfileSettings")
    assert set(profile_settings["properties"]["content_language"]["enum"]) == {
        "ru",
        "en",
    }
    assert set(profile_settings["properties"]["comment_permission"]["enum"]) == {
        "everyone",
        "followers",
        "nobody",
    }
    assert {
        "notifications_in_app",
        "notifications_email",
    }.issubset(profile_settings["required"])
    assert not any(path.startswith("/api/character/") for path in document["paths"])
    assert (
        document["paths"]["/api/projects/{projectId}/"]["get"]["operationId"]
        == "getProject"
    )
    missing_characters_operation = document["paths"][
        "/api/projects/{projectId}/scenes/missing-characters/"
    ]["get"]
    assert missing_characters_operation["operationId"] == "listProjectMissingCharacters"
    assert missing_characters_operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/MissingCharactersResponse"}
    missing_character = _schema(document, "MissingCharacter")
    assert set(missing_character["required"]) == {
        "name",
        "dialogueCount",
        "sceneCount",
    }
    missing_characters_response = _schema(document, "MissingCharactersResponse")
    assert set(missing_characters_response["required"]) == {"characters"}
    assert missing_characters_response["properties"]["characters"]["items"] == {
        "$ref": "#/components/schemas/MissingCharacter"
    }
    video_preparation_operation = document["paths"][
        "/api/projects/{projectId}/video/preparation/"
    ]["get"]
    assert video_preparation_operation["operationId"] == "getProjectVideoPreparation"
    assert video_preparation_operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/VideoPreparationResponse"}
    video_preparation = _schema(document, "VideoPreparationResponse")
    assert set(video_preparation["required"]) == {
        "project",
        "ready",
        "taskCount",
        "missingCharacters",
        "emptyScenes",
        "storyboard",
    }
    assert video_preparation["properties"]["storyboard"] == {
        "$ref": "#/components/schemas/VideoPreparationStoryboard"
    }
    project_readiness = _schema(document, "ProjectReadiness")
    assert "videoPreparation" in project_readiness["required"]
    assert project_readiness["properties"]["videoPreparation"] == {
        "$ref": "#/components/schemas/VideoPreparationCompact"
    }
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

    scene_storyboard = _schema(document, "SceneStoryboard")
    assert scene_storyboard["properties"]["assetId"]["nullable"] is True
    storyboard_workspace = _schema(document, "StoryboardWorkspace")
    assert storyboard_workspace["properties"]["shots"] == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/StoryboardShot"},
    }
    shot_proposal = _schema(document, "StoryboardShotProposal")
    assert {"shots", "source"}.issubset(shot_proposal["required"])
    assert shot_proposal["properties"]["source"] == {
        "$ref": "#/components/schemas/StoryboardShotListSource",
    }
    proposed_shot = shot_proposal["properties"]["shots"]["items"]
    assert "source_segment_ids" in proposed_shot["required"]
    assert proposed_shot["properties"]["source_segment_ids"]["uniqueItems"] is True
    shot_source = _schema(document, "StoryboardShotListSource")
    assert set(shot_source["required"]) == {
        "scene_id", "scene_version", "content_hash", "segments", "truncated",
    }
    assert shot_source["properties"]["segments"]["items"] == {
        "$ref": "#/components/schemas/StoryboardSourceSegment",
    }
    draft_payload = _schema(document, "StoryboardEditorDraftPayload")
    assert set(draft_payload["required"]) == {"schemaVersion", "stage", "shots"}
    assert draft_payload["additionalProperties"] is False
    assert draft_payload["properties"]["shots"]["maxItems"] == 500
    assert draft_payload["properties"]["stage"]["enum"] == [
        "selection", "builder", "editor",
    ]
    draft_mutation = _schema(document, "StoryboardEditorDraftMutation")
    assert set(draft_mutation["required"]) == {
        "expectedRevision", "mutationId", "payload",
    }
    assert draft_mutation["properties"]["mutationId"]["format"] == "uuid"
    assert "imageUrl" not in _schema(document, "StoryboardEditorDraftKeyframe")[
        "properties"
    ]
    assert "imageUrl" not in _schema(document, "StoryboardEditorDraftReference")[
        "properties"
    ]
    draft_composition = _schema(document, "StoryboardEditorDraftCameraIntent")[
        "properties"
    ]["composition"]["items"]["properties"]
    for coordinate in ("x", "y", "width", "height"):
        assert draft_composition[coordinate]["minimum"] == 0
        assert draft_composition[coordinate]["maximum"] == 100
    draft_path = document["paths"][
        "/api/projects/{projectId}/storyboard/scenes/{sceneId}/editor-draft/"
    ]["put"]
    assert "409" in draft_path["responses"]
    assert _schema(document, "StoryboardSuggestShotsRequest")["properties"][
        "language"
    ]["enum"] == ["ru", "en"]
    shot_list_job = _schema(document, "StoryboardShotListJob")
    assert shot_list_job["properties"]["status"]["enum"] == [
        "queued", "running", "succeeded", "failed",
    ]
    assert shot_list_job["properties"]["resultState"]["enum"] == [
        "pending", "applied", "dismissed",
    ]
    assert {"jobId", "result", "appliedRevision", "expectedRevision"}.issubset(
        shot_list_job["required"]
    )
    shot_list_start = _schema(document, "StoryboardShotListJobCreate")
    assert shot_list_start["required"] == ["requestId"]
    assert shot_list_start["properties"]["requestId"]["format"] == "uuid"
    assert shot_list_start["properties"]["estimatedSeconds"]["maximum"] == 3600
    shot_list_start_path = document["paths"][
        "/api/projects/{projectId}/storyboard/scenes/{sceneId}/shot-list-jobs/"
    ]["post"]
    assert "202" in shot_list_start_path["responses"]
    assert set(_schema(document, "StoryboardShotListJobApply")["required"]) == {
        "expectedRevision", "mutationId",
    }
    create_shot_operation = document["paths"][
        "/api/projects/{projectId}/storyboard/{storyboardId}/shots/"
    ]["post"]
    assert create_shot_operation["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/StoryboardShotCreateMutation"}
    patch_shot_operation = document["paths"][
        "/api/projects/{projectId}/storyboard/shots/{shotId}/"
    ]["patch"]
    assert patch_shot_operation["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/StoryboardShotPatchMutation"}
    assert "expectedVersion" in _schema(
        document,
        "StoryboardShotPatchMutation",
    )["required"]
    regenerate_operation = document["paths"][
        "/api/projects/{projectId}/storyboard/keyframes/{keyframeId}/regenerate/"
    ]["post"]
    assert regenerate_operation["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/StoryboardGenerateRequest"}
    assert set(_schema(document, "StoryboardAzimuth")["enum"]) == {
        "front",
        "front_left",
        "left",
        "back_left",
        "back",
        "back_right",
        "right",
        "front_right",
    }
    assert set(_schema(document, "StoryboardMovement")["enum"]) == {
        "static",
        "dolly_in",
        "dolly_out",
        "pan_left",
        "pan_right",
        "tilt_up",
        "tilt_down",
        "orbit_left",
        "orbit_right",
        "truck_left",
        "truck_right",
        "crane_up",
        "crane_down",
        "follow",
        "custom",
    }


if __name__ == "__main__":
    check_contract()
    print("OpenAPI contract checks passed")
