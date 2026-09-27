import uuid
from copy import deepcopy

from django.test import TestCase

from w_craft_back.character_studio.models import CharacterAsset, StudioCharacter
from w_craft_back.movie.project.dashboard_models import (
    ProjectAsset, Scene, SceneStoryboard,
)
from w_craft_back.movie.storyboard.editor_frames import frame_input
from w_craft_back.movie.storyboard.models import (
    CameraIntent, KeyframeGenerationReference, SceneStoryboardEditorDraft,
    StoryboardEditorFrameJob, StoryboardKeyframe, StoryboardKeyframeGeneration,
    StoryboardShot,
)
from w_craft_back.movie.storyboard.generation import build_generation_snapshot
from w_craft_back.movie.storyboard.tests.test_api import make_project, make_user
from w_craft_back.movie.storyboard.tests.test_editor_drafts import make_payload
from w_craft_back.movie.video.sources import _hash, collect_sources


class VideoSourcesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, _ = make_user("video-source-owner")
        cls.project = make_project(cls.user, "Source film")

    def setUp(self):
        self.scene = Scene.objects.create(project=self.project, order=1,
                                          title="Ready", script_text="An empty room.")
        payload = make_payload(self.scene)
        payload["shots"][0]["keyframes"][0]["generationReferences"] = []
        self.draft = SceneStoryboardEditorDraft.objects.create(
            scene=self.scene, payload=payload, last_mutation_id=uuid.uuid4())
        self.asset = ProjectAsset.objects.create(
            project=self.project, asset_type="storyboard", file="test/frame.png",
            metadata={"mime_type": "image/png"})
        self.frame_job = self.add_frame(self.draft.payload["shots"][0])

    def add_frame(self, shot):
        frame = shot["keyframes"][0]
        return StoryboardEditorFrameJob.objects.create(
            scene=self.scene, actor=self.user, shot_id=shot["id"],
            keyframe_id=frame["id"], request_id=uuid.uuid4(),
            request_parameters={}, expected_revision=1, request_snapshot={},
            provider_snapshot={}, model="test", asset=self.asset, status="succeeded",
            input_fingerprint=_hash(frame_input(
                self.draft.payload, shot["id"], frame["id"])))

    def source(self):
        return collect_sources(self.project)[0]["shots"][0]

    def save_draft(self):
        self.draft.save(update_fields=["payload"])

    def test_ready_draft_scene_does_not_wait_for_other_scene(self):
        Scene.objects.create(project=self.project, order=2, title="Unprepared")
        scenes = collect_sources(self.project)
        self.assertTrue(scenes[0]["ready"], scenes[0]["shots"][0]["blockers"])
        self.assertFalse(scenes[1]["ready"])
        self.assertEqual(scenes[0]["shots"][0]["startFrame"]["assetId"], self.asset.pk)

    def test_entire_selected_scene_must_be_ready(self):
        second = deepcopy(self.draft.payload["shots"][0])
        second.update(id="second-shot", order=2)
        second["keyframes"] = []
        self.draft.payload["shots"].append(second)
        self.save_draft()
        self.assertFalse(self.source()["ready"])

    def test_ignored_intermediate_frame_needs_no_image(self):
        shot = self.draft.payload["shots"][0]
        shot["keyframes"].append({"id": "middle", "type": "intermediate"})
        self.save_draft()
        source = self.source()
        self.assertTrue(source["ready"], source["blockers"])
        self.assertEqual(source["intermediateFrameCount"], 1)

    def test_current_draft_wins_over_legacy_accepted_storyboard(self):
        SceneStoryboard.objects.create(
            scene=self.scene, asset=self.asset,
            source_scene_version=self.scene.version)
        self.draft.payload["shots"] = []
        self.save_draft()
        self.assertEqual(collect_sources(self.project)[0]["shots"], [])

    def test_changed_frame_conditions_are_not_satisfied_by_old_image(self):
        before = self.source()["fingerprint"]
        self.draft.payload["shots"][0]["keyframes"][0]["cameraIntent"]["lens"] = 85
        self.save_draft()
        source = self.source()
        self.assertFalse(source["ready"])
        self.assertIsNone(source["startFrame"])
        self.assertNotEqual(source["fingerprint"], before)

    def test_sibling_shot_edits_do_not_invalidate_current_clip(self):
        before = self.source()["fingerprint"]
        second = deepcopy(self.draft.payload["shots"][0])
        second.update(id="neighbor", order=2, description="Other shot")
        second["keyframes"][0].update(id="other-frame", shotId="neighbor")
        self.draft.payload["shots"].append(second)
        self.save_draft()
        self.add_frame(second)
        self.assertEqual(self.source()["fingerprint"], before)

    def test_reordering_scene_does_not_invalidate_saved_script(self):
        before = self.source()["fingerprint"]
        self.scene.order = 3
        self.scene.version += 1
        self.scene.save(update_fields=["order", "version"])
        source = self.source()
        self.assertTrue(source["ready"], source["blockers"])
        self.assertEqual(source["fingerprint"], before)

    def test_foreign_generated_asset_cannot_become_a_condition(self):
        other = make_project(self.user, "Other")
        asset = ProjectAsset.objects.create(
            project=other, file="private/frame.png", asset_type="storyboard")
        StoryboardEditorFrameJob.objects.filter(
            pk=self.frame_job.pk).update(asset=asset)
        self.assertFalse(self.source()["ready"])
        self.assertIsNone(self.source()["startFrame"])

    def test_character_appearance_and_script_change_fingerprint(self):
        character = StudioCharacter.objects.create(
            project=self.project, name="Anna", status="active")
        CharacterAsset.objects.create(
            project=self.project, character=character, asset_type="portrait",
            status="ready", storage_path="private/anna.png", mime_type="image/png")
        self.scene.scene_characters.create(character=character)
        before = self.source()["fingerprint"]
        character.age = 40
        character.save(update_fields=["age"])
        after_character = self.source()["fingerprint"]
        self.assertNotEqual(before, after_character)
        self.scene.script_text = "Anna leaves."
        self.scene.save(update_fields=["script_text"])
        self.assertNotEqual(after_character, self.source()["fingerprint"])

    def test_structured_boundary_reference_changes_are_tracked(self):
        self.draft.delete()
        character = StudioCharacter.objects.create(
            project=self.project, name="Anna", status="active")
        portrait = CharacterAsset.objects.create(
            project=self.project, character=character, asset_type="portrait",
            status="ready", storage_path="private/anna.png", mime_type="image/png")
        character.canonical_reference_image = portrait
        character.save(update_fields=["canonical_reference_image"])
        storyboard = SceneStoryboard.objects.create(
            scene=self.scene, source_scene_version=self.scene.version)
        shot = StoryboardShot.objects.create(
            storyboard=storyboard, order=1, title="Portrait",
            description="Anna walks.", duration_seconds=5)
        frame = StoryboardKeyframe.objects.create(shot=shot, type="start", position=0)
        CameraIntent.objects.create(keyframe=frame)
        KeyframeGenerationReference.objects.create(
            keyframe=frame, character=character, reference_type="character")
        snapshot, fingerprint = build_generation_snapshot(frame)
        generation = StoryboardKeyframeGeneration.objects.create(
            keyframe=frame, actor=self.user, status="ready", asset=self.asset,
            request_snapshot=snapshot, request_fingerprint=fingerprint)
        frame.current_generation = generation
        frame.save(update_fields=["current_generation"])
        source = self.source()
        self.assertTrue(source["ready"], source["blockers"])
        self.assertEqual(source["references"][0]["id"], str(character.pk))
        character.age = 40
        character.save(update_fields=["age"])
        self.assertNotEqual(source["fingerprint"], self.source()["fingerprint"])
