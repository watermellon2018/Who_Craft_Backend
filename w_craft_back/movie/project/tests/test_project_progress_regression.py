"""Regression coverage for scenario-derived character appearances."""

from fractions import Fraction

from django.contrib.auth.models import User
from django.test import TestCase

from w_craft_back.character_studio.models import StudioCharacter
from w_craft_back.movie.project.dashboard_models import Scene
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.progress_service import calculate_project_progress


class ScenarioCharacterAppearanceProgressTests(TestCase):
    def test_dialogue_character_counts_scene_without_explicit_link(self):
        owner = User.objects.create_user("scenario-character-owner")
        project = Project.objects.create(
            owner=owner,
            title="Scenario character progress",
            format="feature_film",
            annotation="",
            synopsis="",
        )
        character = StudioCharacter.objects.create(project=project, name="Анна")
        Scene.objects.create(
            project=project,
            title="Сцена 1",
            order=1,
            script_blocks=[
                {
                    "id": f"dialogue-{index}",
                    "type": "dialogue",
                    "text": f"Реплика {index}",
                    "characterId": str(character.character_id),
                }
                for index in range(6)
            ],
        )

        snapshot = calculate_project_progress(project)

        self.assertEqual(snapshot.characters, Fraction(0, 1))
        self.assertEqual(snapshot.overall, Fraction(1, 5))
