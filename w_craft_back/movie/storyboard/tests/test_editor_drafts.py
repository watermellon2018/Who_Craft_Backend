from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
    Scene,
)
from w_craft_back.movie.storyboard.models import (
    SceneStoryboardEditorDraft,
    StoryboardShot,
)
from w_craft_back.movie.storyboard.tests.test_api import make_project, make_user
from w_craft_back.profile.models import UserProfile


def make_payload(scene: Scene) -> dict:
    text = scene.script_text
    return {
        "schemaVersion": 1,
        "stage": "selection",
        "shots": [{
            "id": "shot-local-1", "sceneId": str(scene.pk), "order": 1,
            "title": "Анна входит", "description": "Анна замечает письмо.",
            "duration": 4, "characterIds": [], "referenceIds": [],
            "keyframes": [{
                "id": "keyframe-1", "shotId": "shot-local-1", "position": 0,
                "type": "start", "generationStatus": "idle",
                "cameraIntent": {
                    "azimuth": "front-left", "elevation": "eye-level",
                    "distance": "medium", "framing": "medium-close", "lens": 50,
                    "composition": [{
                        "subjectId": "actor-1", "x": 58, "y": 27,
                        "width": 28, "height": 48,
                    }],
                    "ots": {"shoulder": "left", "targetId": "actor-1"},
                },
                "generationReferences": [{
                    "id": "actor-1", "type": "character", "title": "Анна",
                    "primary": True,
                }],
            }],
            "transitions": [],
            "source": {
                "origin": "manual", "segmentIds": [],
                "ranges": [{"start": 0, "end": 1}],
                "document": {
                    "sceneId": scene.pk, "sceneVersion": scene.version,
                    "contentHash": hashlib.sha256(text.encode()).hexdigest(),
                    "segments": [{"id": "segment-1", "text": text}],
                    "truncated": False,
                },
            },
        }],
    }


class EditorDraftApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner, cls.owner_key = make_user("draft-owner")
        cls.viewer, cls.viewer_key = make_user("draft-viewer")
        cls.editor, cls.editor_key = make_user("draft-editor")
        cls.outsider, cls.outsider_key = make_user("draft-outsider")
        cls.project = make_project(cls.owner, "Working storyboard")
        cls.other_project = make_project(cls.outsider, "Other storyboard")
        for user, role in (
            (cls.viewer, ProjectMemberRole.VIEWER),
            (cls.editor, ProjectMemberRole.EDITOR),
        ):
            ProjectMember.objects.create(project=cls.project, user=user, role=role)
        cls.scene = Scene.objects.create(
            project=cls.project, title="Кухня", order=1,
            script_text="😀 Анна входит. Она замечает письмо.",
        )
        cls.foreign_scene = Scene.objects.create(
            project=cls.other_project, title="Other", order=1, script_text="Secret",
        )

    def setUp(self):
        self.client = APIClient()
        self.url = (
            f"/api/projects/{self.project.pk}/storyboard/scenes/"
            f"{self.scene.pk}/editor-draft/"
        )
        self.list_url = f"/api/projects/{self.project.pk}/storyboard/editor-drafts/"

    def token(self, key=None):
        return {"HTTP_X_USER_TOKEN": str((key or self.owner_key).key)}

    def put(self, payload=None, revision=0, mutation=None, key=None, url=None):
        return self.client.put(
            url or self.url,
            {
                "expectedRevision": revision,
                "mutationId": mutation or str(uuid.uuid4()),
                "payload": payload if payload is not None else make_payload(self.scene),
            },
            format="json", **self.token(key),
        )

    def test_roundtrip_partial_manual_work_without_creating_rendered_shots(self):
        payload = make_payload(self.scene)
        response = self.put(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {
            "sceneId": self.scene.pk, "revision": 1, "payload": payload,
        })
        restored = APIClient().get(self.list_url, **self.token())
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json(), {
            "userId": self.owner.pk, "canEdit": True, "drafts": [response.json()],
        })
        self.assertEqual(StoryboardShot.objects.count(), 0)
        self.assertEqual(
            SceneStoryboardEditorDraft.objects.get().updated_by, self.owner,
        )

    def test_viewer_reads_but_cannot_write_and_outsider_has_no_access(self):
        self.assertEqual(self.put().status_code, 200)
        viewed = self.client.get(self.list_url, **self.token(self.viewer_key))
        self.assertEqual(viewed.status_code, 200)
        self.assertFalse(viewed.json()["canEdit"])
        self.assertEqual(viewed.json()["userId"], self.viewer.pk)
        self.assertEqual(self.put(key=self.viewer_key).status_code, 403)
        self.assertEqual(self.put(key=self.outsider_key).status_code, 403)
        self.assertEqual(
            self.client.get(self.list_url, **self.token(self.outsider_key)).status_code,
            403,
        )

    def test_auth_required_and_scene_must_belong_to_project(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 401)
        self.assertEqual(self.client.put(self.url, {}, format="json").status_code, 401)
        foreign_url = self.url.replace(
            f"scenes/{self.scene.pk}/", f"scenes/{self.foreign_scene.pk}/",
        )
        self.assertEqual(self.put(url=foreign_url).status_code, 404)
        payload = make_payload(self.foreign_scene)
        self.assertEqual(self.put(payload).status_code, 400)

    def test_atomic_first_revision_and_stale_writes_do_not_overwrite(self):
        self.assertEqual(self.put().status_code, 200)
        payload = make_payload(self.scene)
        payload["shots"][0]["title"] = "Другой редактор"
        stale = self.put(payload, key=self.editor_key)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "STORYBOARD_DRAFT_CONFLICT")
        self.assertEqual(stale.json()["errors"], {"currentRevision": 1})
        saved = self.put(payload, revision=1, key=self.editor_key)
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["revision"], 2)
        self.assertEqual(
            SceneStoryboardEditorDraft.objects.get().updated_by, self.editor,
        )

    def test_idempotent_retry_then_old_mutation_conflict(self):
        mutation = str(uuid.uuid4())
        first = self.put(mutation=mutation)
        replay = self.put(mutation=mutation)
        self.assertEqual(first.json(), replay.json())
        changed = make_payload(self.scene)
        changed["shots"][0]["description"] += " Изменение."
        self.assertEqual(self.put(changed, mutation=mutation).status_code, 400)
        self.assertEqual(self.put(changed, revision=1).status_code, 200)
        self.assertEqual(self.put(mutation=mutation).status_code, 409)

    def test_empty_working_copy_is_durable(self):
        payload = {"schemaVersion": 1, "stage": "selection", "shots": []}
        self.assertEqual(self.put(payload).json()["payload"], payload)

    def test_rejects_media_credentials_unknown_fields_and_transient_state(self):
        base = make_payload(self.scene)
        for field, value in (
            ("imageUrl", "https://example.test/image.png"),
            ("description", "data:image/png;base64,AAAA"),
            ("description", "blob:https://example.test/1"),
            ("description", "mock://frame"),
            ("description", "https://example.test/file?token=secret"),
            ("description", "https://example.test/file?X-Amz-Signature=secret"),
        ):
            with self.subTest(field=field, value=value.split(":")[0]):
                payload = deepcopy(base)
                payload["shots"][0][field] = value
                response = self.put(payload)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(b"secret", response.content)
        transient = deepcopy(base)
        transient["shots"][0]["keyframes"][0]["generationStatus"] = "loading"
        self.assertEqual(self.put(transient).status_code, 400)
        self.assertFalse(SceneStoryboardEditorDraft.objects.exists())

    def test_rejects_duplicate_ids_and_invalid_transition_targets(self):
        base = make_payload(self.scene)
        duplicate = deepcopy(base)
        duplicate["shots"].append(deepcopy(duplicate["shots"][0]))
        duplicate["shots"][1]["order"] = 2
        self.assertEqual(self.put(duplicate).status_code, 400)
        duplicate["shots"][1]["id"] = "shot-2"
        duplicate["shots"][1]["keyframes"][0]["shotId"] = "shot-2"
        self.assertEqual(self.put(duplicate).status_code, 400)
        invalid = deepcopy(base)
        invalid["shots"][0]["transitions"] = [{
            "id": "transition-1", "fromKeyframeId": "keyframe-1",
            "toKeyframeId": "foreign-keyframe",
        }]
        self.assertEqual(self.put(invalid).status_code, 400)

    def test_ranges_count_unicode_codepoints_and_overlap_is_allowed(self):
        payload = make_payload(self.scene)
        source = payload["shots"][0]["source"]
        source["ranges"] = [{"start": 0, "end": 1}, {"start": 0, "end": 5}]
        response = self.put(payload)
        self.assertEqual(response.status_code, 200, response.content)
        source["ranges"] = [{"start": 0, "end": len(self.scene.script_text) + 1}]
        self.assertEqual(self.put(payload, revision=1).status_code, 400)

    def test_source_hash_scene_and_current_text_are_validated(self):
        for field, value in (
            ("contentHash", "0" * 64),
            ("sceneId", self.foreign_scene.pk),
            ("sceneVersion", self.scene.version + 1),
        ):
            with self.subTest(field=field):
                payload = make_payload(self.scene)
                payload["shots"][0]["source"]["document"][field] = value
                self.assertEqual(self.put(payload).status_code, 400)
        payload = make_payload(self.scene)
        document = payload["shots"][0]["source"]["document"]
        document["segments"][0]["text"] = "Unrelated but internally consistent"
        document["contentHash"] = hashlib.sha256(
            document["segments"][0]["text"].encode(),
        ).hexdigest()
        self.assertEqual(self.put(payload).status_code, 400)

    def test_stale_source_snapshot_is_preserved_after_screenplay_edit(self):
        payload = make_payload(self.scene)
        Scene.objects.filter(pk=self.scene.pk).update(
            version=2, script_text="Новый текст",
        )
        response = self.put(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["payload"], payload)

    def test_size_limit_and_nonfinite_numbers_are_rejected(self):
        payload = make_payload(self.scene)
        with patch("w_craft_back.movie.storyboard.editor_drafts.MAX_PAYLOAD_BYTES", 10):
            self.assertEqual(self.put(payload).status_code, 400)
        payload["shots"][0]["duration"] = "NaN"
        self.assertEqual(self.put(payload).status_code, 400)

    def test_composition_percentage_boundaries_roundtrip_without_rescaling(self):
        payload = make_payload(self.scene)
        composition = payload["shots"][0]["keyframes"][0]["cameraIntent"][
            "composition"
        ]
        composition[0].update(x=0, y=0, width=100, height=100)
        saved = self.put(payload)
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["payload"], payload)
        for field in ("x", "y", "width", "height"):
            for value in (-0.01, 100.01):
                with self.subTest(field=field, value=value):
                    invalid = deepcopy(payload)
                    invalid["shots"][0]["keyframes"][0]["cameraIntent"][
                        "composition"
                    ][0][field] = value
                    self.assertEqual(self.put(invalid, revision=1).status_code, 400)

    @patch("w_craft_back.movie.storyboard.views.AIShotListService")
    def test_generation_language_uses_profile_and_explicit_override(self, service):
        service.return_value.suggest.return_value = {"shots": []}
        service.options.return_value = {"models": []}
        UserProfile.objects.update_or_create(
            user=self.owner, defaults={"language": "ru", "content_language": "en"},
        )
        url = self.url.replace("editor-draft/", "suggest-shots/")
        response = self.client.post(url, {}, format="json", **self.token())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            service.return_value.suggest.call_args.kwargs["language"], "ru",
        )
        response = self.client.post(
            url, {"language": "en"}, format="json", **self.token(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            service.return_value.suggest.call_args.kwargs["language"], "en",
        )
        self.assertEqual(
            self.client.get(url, {"language": "ru"}, **self.token()).status_code, 200,
        )
        self.assertEqual(service.options.call_args.kwargs["language"], "ru")
        invalid = self.client.post(
            url, {"language": "zz"}, format="json", **self.token(),
        )
        self.assertEqual(invalid.status_code, 400)


class EditorDraftConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_first_saves_create_one_draft_and_return_conflict(self):
        owner, key = make_user("concurrent-draft-owner")
        project = make_project(owner, "Concurrent draft")
        scene = Scene.objects.create(project=project, title="Scene", order=1)
        url = (
            f"/api/projects/{project.pk}/storyboard/scenes/"
            f"{scene.pk}/editor-draft/"
        )
        barrier = Barrier(2)

        def save(stage: str) -> tuple[int, dict]:
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = APIClient().put(
                    url,
                    {
                        "expectedRevision": 0, "mutationId": str(uuid.uuid4()),
                        "payload": {"schemaVersion": 1, "stage": stage, "shots": []},
                    },
                    format="json", HTTP_X_USER_TOKEN=str(key.key),
                )
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(save, ("selection", "builder")))

        self.assertEqual(sorted(result[0] for result in results), [200, 409])
        self.assertEqual(SceneStoryboardEditorDraft.objects.count(), 1)
        draft = SceneStoryboardEditorDraft.objects.get()
        self.assertEqual(draft.revision, 1)
        accepted = next(payload for status, payload in results if status == 200)
        self.assertEqual(draft.payload, accepted["payload"])
