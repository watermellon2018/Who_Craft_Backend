from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from w_craft_back.movie.storyboard.domain import (
    CameraMovementResolver,
    ContinuityReferenceService,
    ShotReadinessService,
    ordered_keyframes,
    rebuild_transitions,
    recalculate_adjacent_transitions,
)
from w_craft_back.movie.storyboard.models import (
    CameraIntent,
    CameraTransition,
    StoryboardKeyframe,
    StoryboardShot,
)
from w_craft_back.movie.storyboard.worker import StoryboardImageProviderAdapter
from w_craft_back.movie.project.dashboard_models import Scene, SceneStoryboard
from w_craft_back.movie.project.models import Project


def camera_intent(**overrides):
    values = {
        "azimuth": "front",
        "elevation": "eye_level",
        "distance": "medium",
        "framing": "medium",
        "lens_mm": 50,
        "target": {"type": "character", "ids": ["anna"]},
        "composition": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def keyframe(
    identifier,
    frame_type,
    position,
    *,
    camera=True,
    generation_status="ready",
):
    values = {
        "id": identifier,
        "pk": identifier,
        "type": frame_type,
        "position": position,
        "current_generation": (
            SimpleNamespace(status=generation_status)
            if generation_status is not None
            else None
        ),
    }
    if camera:
        values["camera_intent"] = camera_intent()
    return SimpleNamespace(**values)


class StoryboardProviderTimeoutTests(SimpleTestCase):
    @override_settings(
        STORYBOARD_PROVIDER_TIMEOUT_SECONDS=120,
        STORYBOARD_JOB_LEASE_SECONDS=90,
    )
    def test_route_candidates_share_one_provider_timeout_budget(self):
        class RoutedProvider:
            specs = (object(), object())

            def generate(self, prompt, **kwargs):
                self.timeout = kwargs["timeout"]
                return [b"image"]

        provider = RoutedProvider()

        result = StoryboardImageProviderAdapter.generate(
            provider,
            {"compiledPrompt": "A storyboard frame"},
        )

        self.assertEqual(result, [b"image"])
        self.assertEqual(provider.timeout, 60)


class CameraMovementResolverTests(SimpleTestCase):
    def test_identical_essential_intents_are_static(self):
        start = camera_intent(framing="wide", lens_mm=35)
        end = camera_intent(framing="close", lens_mm=85)

        result = CameraMovementResolver.resolve(start, end)

        self.assertEqual(result["movement"], "static")
        self.assertEqual(result["metadata"]["changes"], [])

    def test_distance_change_resolves_dolly_in_and_out(self):
        wide = camera_intent(distance="wide")
        medium = camera_intent(distance="medium")
        near = camera_intent(distance="near")

        self.assertEqual(
            CameraMovementResolver.resolve(wide, medium)["movement"],
            "dolly_in",
        )
        self.assertEqual(
            CameraMovementResolver.resolve(near, medium)["movement"],
            "dolly_out",
        )

    def test_azimuth_ring_resolves_orbit_direction(self):
        front = camera_intent(azimuth="front")
        right = camera_intent(azimuth="right")
        left = camera_intent(azimuth="left")

        self.assertEqual(
            CameraMovementResolver.resolve(front, right)["movement"],
            "orbit_right",
        )
        self.assertEqual(
            CameraMovementResolver.resolve(front, left)["movement"],
            "orbit_left",
        )

    def test_elevation_change_resolves_crane_direction(self):
        low = camera_intent(elevation="low")
        high = camera_intent(elevation="high")

        self.assertEqual(
            CameraMovementResolver.resolve(low, high)["movement"],
            "crane_up",
        )
        self.assertEqual(
            CameraMovementResolver.resolve(high, low)["movement"],
            "crane_down",
        )

    def test_target_change_uses_composition_for_pan_direction(self):
        composition = [
            {
                "subject_id": "anna",
                "x": 0.1,
                "y": 0.1,
                "width": 0.2,
                "height": 0.8,
            },
            {
                "subject_id": "max",
                "x": 0.7,
                "y": 0.1,
                "width": 0.2,
                "height": 0.8,
            },
        ]
        anna = camera_intent(
            target={"type": "character", "ids": ["anna"]},
            composition=composition,
        )
        max_target = camera_intent(
            target={"type": "character", "ids": ["max"]},
            composition=composition,
        )

        self.assertEqual(
            CameraMovementResolver.resolve(anna, max_target)["movement"],
            "pan_right",
        )
        self.assertEqual(
            CameraMovementResolver.resolve(max_target, anna)["movement"],
            "pan_left",
        )

    def test_target_change_without_composition_is_custom(self):
        result = CameraMovementResolver.resolve(
            camera_intent(target={"type": "character", "ids": ["anna"]}),
            camera_intent(target={"type": "character", "ids": ["max"]}),
        )

        self.assertEqual(result["movement"], "custom")
        self.assertEqual(result["metadata"]["reason"], "insufficient_composition")

    def test_multiple_primary_changes_are_custom_with_sorted_changes(self):
        result = CameraMovementResolver.resolve(
            camera_intent(azimuth="front", distance="wide"),
            camera_intent(azimuth="right", distance="near"),
        )

        self.assertEqual(result["movement"], "custom")
        self.assertEqual(result["metadata"]["changes"], ["azimuth", "distance"])


class StoryboardDomainServiceTests(SimpleTestCase):
    def test_keyframes_are_ordered_by_position(self):
        frames = [
            keyframe("end", "end", 1),
            keyframe("middle", "intermediate", 0.5),
            keyframe("start", "start", 0),
        ]

        self.assertEqual(
            [frame.id for frame in ordered_keyframes(frames)],
            ["start", "middle", "end"],
        )

    def test_shot_readiness_requires_boundary_intents_and_ready_generations(self):
        ready_shot = SimpleNamespace(
            keyframes=[
                keyframe("start", "start", 0),
                keyframe("end", "end", 1),
            ]
        )

        self.assertEqual(
            ShotReadinessService.evaluate(ready_shot),
            {"ready": True, "missing": []},
        )

        not_ready_shot = SimpleNamespace(
            keyframes=[
                keyframe("start", "start", 0),
                keyframe(
                    "end",
                    "end",
                    1,
                    camera=False,
                    generation_status="failed",
                ),
            ]
        )
        self.assertEqual(
            ShotReadinessService.evaluate(not_ready_shot),
            {
                "ready": False,
                "missing": ["end_camera_intent", "end_image"],
            },
        )

    def test_storyboard_status_is_computed_from_shots(self):
        ready_shot = SimpleNamespace(
            keyframes=[
                keyframe("ready-start", "start", 0),
                keyframe("ready-end", "end", 1),
            ]
        )
        draft_shot = SimpleNamespace(
            keyframes=[keyframe("draft-start", "start", 0)]
        )

        self.assertEqual(
            ShotReadinessService.storyboard_status(SimpleNamespace(shots=[])),
            "empty",
        )
        self.assertEqual(
            ShotReadinessService.storyboard_status(
                SimpleNamespace(shots=[ready_shot, draft_shot])
            ),
            "draft",
        )
        self.assertEqual(
            ShotReadinessService.storyboard_status(
                SimpleNamespace(shots=[ready_shot])
            ),
            "completed",
        )

    def test_continuity_suggestions_follow_frame_semantics(self):
        storyboard = SimpleNamespace(shots=[])
        first_start = keyframe("first-start", "start", 0)
        first_end = keyframe("first-end", "end", 1)
        first_shot = SimpleNamespace(
            id="first-shot",
            order=1,
            storyboard=storyboard,
            keyframes=[first_start, first_end],
        )
        second_start = keyframe("second-start", "start", 0)
        middle = keyframe("middle", "intermediate", 0.5)
        second_end = keyframe("second-end", "end", 1)
        second_shot = SimpleNamespace(
            id="second-shot",
            order=2,
            storyboard=storyboard,
            keyframes=[second_start, middle, second_end],
        )
        storyboard.shots = [first_shot, second_shot]
        for frame in first_shot.keyframes:
            frame.shot = first_shot
        for frame in second_shot.keyframes:
            frame.shot = second_shot

        self.assertEqual(
            ContinuityReferenceService.suggest(second_start),
            [
                {
                    "type": "previous_shot",
                    "keyframe_id": "first-end",
                    "reason": "Previous shot end frame",
                }
            ],
        )
        self.assertEqual(
            ContinuityReferenceService.suggest(middle)[0]["keyframe_id"],
            "second-start",
        )
        self.assertEqual(
            ContinuityReferenceService.suggest(second_end)[0]["keyframe_id"],
            "middle",
        )

    def test_end_continuity_falls_back_to_start(self):
        shot = SimpleNamespace(
            keyframes=[
                keyframe("start", "start", 0),
                keyframe("end", "end", 1),
            ]
        )
        for frame in shot.keyframes:
            frame.shot = shot

        self.assertEqual(
            ContinuityReferenceService.suggest(shot.keyframes[-1]),
            [
                {
                    "type": "previous_keyframe",
                    "keyframe_id": "start",
                    "reason": "Shot start frame",
                }
            ],
        )


class TransitionDomainTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user = get_user_model().objects.create_user(
            username="storyboard-domain",
            password="pw",
        )
        project = Project.objects.create(
            title="Storyboard domain",
            format="other",
            annotation="",
            synopsis="",
            owner=user,
        )
        scene = Scene.objects.create(
            project=project,
            title="Scene",
            order=1,
            created_by=user,
            updated_by=user,
        )
        cls.storyboard = SceneStoryboard.objects.create(
            scene=scene,
            source_scene_version=scene.version,
            created_by=user,
            updated_by=user,
        )

    def create_shot(self):
        return StoryboardShot.objects.create(
            storyboard=self.storyboard,
            order=self.storyboard.shots.count() + 1,
            title="Shot",
        )

    def create_keyframe(self, shot, frame_type, position, **intent_overrides):
        frame = StoryboardKeyframe.objects.create(
            shot=shot,
            type=frame_type,
            position=Decimal(str(position)),
        )
        intent_values = {
            "target": {"type": "character", "ids": ["anna"]},
            "azimuth": "front",
            "elevation": "eye_level",
            "distance": "medium",
            "framing": "medium",
            "composition": [],
        }
        intent_values.update(intent_overrides)
        CameraIntent.objects.create(keyframe=frame, **intent_values)
        return frame

    def test_rebuild_creates_only_adjacent_edges_and_preserves_surviving_override(self):
        shot = self.create_shot()
        start = self.create_keyframe(shot, "start", 0, distance="wide")
        end = self.create_keyframe(shot, "end", 1, distance="near")
        original = CameraTransition.objects.create(
            shot=shot,
            from_keyframe=start,
            to_keyframe=end,
            detected_movement="dolly_in",
            override_movement="follow",
        )

        rebuilt = rebuild_transitions(shot)

        self.assertEqual([transition.pk for transition in rebuilt], [original.pk])
        original.refresh_from_db()
        self.assertEqual(original.override_movement, "follow")

        intermediate = self.create_keyframe(
            shot,
            "intermediate",
            Decimal("0.5"),
            distance="medium",
        )
        rebuilt = rebuild_transitions(shot)

        self.assertEqual(
            [
                (transition.from_keyframe_id, transition.to_keyframe_id)
                for transition in rebuilt
            ],
            [(start.pk, intermediate.pk), (intermediate.pk, end.pk)],
        )
        self.assertFalse(CameraTransition.objects.filter(pk=original.pk).exists())
        self.assertTrue(
            all(transition.override_movement is None for transition in rebuilt)
        )

    def test_camera_update_recalculates_only_neighboring_edges(self):
        shot = self.create_shot()
        start = self.create_keyframe(shot, "start", 0, distance="wide")
        first = self.create_keyframe(shot, "intermediate", "0.3")
        current = self.create_keyframe(shot, "intermediate", "0.6")
        end = self.create_keyframe(shot, "end", 1, distance="near")
        rebuild_transitions(shot)
        untouched = CameraTransition.objects.get(
            shot=shot,
            from_keyframe=start,
            to_keyframe=first,
        )
        untouched.metadata = {"sentinel": True}
        untouched.override_movement = "follow"
        untouched.save(update_fields=["metadata", "override_movement", "updated_at"])

        current.camera_intent.elevation = "high"
        current.camera_intent.save()
        result = recalculate_adjacent_transitions(current)

        untouched.refresh_from_db()
        self.assertEqual(untouched.metadata, {"sentinel": True})
        self.assertEqual(untouched.override_movement, "follow")
        self.assertEqual(result["from_previous"].from_keyframe_id, first.pk)
        self.assertEqual(result["to_next"].to_keyframe_id, end.pk)
