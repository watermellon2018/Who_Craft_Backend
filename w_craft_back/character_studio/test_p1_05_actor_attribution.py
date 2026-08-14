from django.contrib.auth.models import User
from django.test import TestCase

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterAssetType,
    RevisionChangeType,
    StudioCharacter,
)
from w_craft_back.character_studio.services.asset_service import (
    CharacterAssetService,
)
from w_craft_back.character_studio.services.revision_service import (
    CharacterRevisionService,
)
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.policy import Action


class CharacterActorAttributionTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(username="actor-owner")
        self.owner_key = UserKey.objects.create(user=owner)
        self.actor = User.objects.create_user(username="actor-editor")
        self.actor_key = UserKey.objects.create(user=self.actor)
        self.project = Project.objects.create(
            owner=owner,
            title="Shared character",
            format="feature_film",
            annotation="",
            synopsis="",
        )
        ProjectMember.objects.create(
            project=self.project,
            user=owner,
            role=ProjectMemberRole.OWNER,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.actor,
            role=ProjectMemberRole.EDITOR,
        )
        self.character = StudioCharacter.objects.create(
            project=self.project,
            user=self.owner_key,
            name="Character",
        )

    def test_revision_records_current_actor(self):
        revision = CharacterRevisionService().create_revision(
            self.actor_key,
            Action.EDIT_CONTENT,
            self.character,
            RevisionChangeType.MANUAL_UPDATE,
        )

        self.assertEqual(revision.user, self.actor_key)

    def test_generated_asset_records_current_actor_and_action(self):
        asset = CharacterAssetService().save_asset(
            self.actor_key,
            Action.RUN_GENERATION,
            self.character,
            CharacterAssetType.PORTRAIT,
            image_url="/media/portrait.png",
        )
        self.assertEqual(asset.user, self.actor_key)

        with self.assertRaises(ValueError):
            CharacterAssetService().save_asset(
                self.actor_key,
                Action.EDIT_CONTENT,
                self.character,
                CharacterAssetType.PORTRAIT,
                image_url="/media/invalid.png",
            )
