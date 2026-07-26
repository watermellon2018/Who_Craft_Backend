"""Regression tests for P0-04 character-studio project permissions."""

import os
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import CharacterGenerationJob
from w_craft_back.character_studio.services.character_service import CharacterService
from w_craft_back.character_studio.services.errors import PermissionDeniedError
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.character_studio.services.permissions import (
    get_project_for_action,
)
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project
from w_craft_back.movie.project.policy import Action


class CharacterStudioPermissionMatrixTests(TestCase):
    ROLE_EXPECTATIONS = {
        "owner": {
            Action.VIEW: True,
            Action.EDIT_CONTENT: True,
            Action.RUN_GENERATION: True,
            Action.EDIT_SETTINGS: True,
        },
        "admin": {
            Action.VIEW: True,
            Action.EDIT_CONTENT: True,
            Action.RUN_GENERATION: True,
            Action.EDIT_SETTINGS: True,
        },
        "editor": {
            Action.VIEW: True,
            Action.EDIT_CONTENT: True,
            Action.RUN_GENERATION: True,
            Action.EDIT_SETTINGS: False,
        },
        "viewer": {
            Action.VIEW: True,
            Action.EDIT_CONTENT: False,
            Action.RUN_GENERATION: False,
            Action.EDIT_SETTINGS: False,
        },
        "outsider": {
            Action.VIEW: False,
            Action.EDIT_CONTENT: False,
            Action.RUN_GENERATION: False,
            Action.EDIT_SETTINGS: False,
        },
    }

    def setUp(self):
        self.previous_provider = os.environ.get("CHARACTER_STUDIO_IMAGE_PROVIDER")
        os.environ["CHARACTER_STUDIO_IMAGE_PROVIDER"] = "mock"
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        self.users = {}
        self.principals = {}
        for role in self.ROLE_EXPECTATIONS:
            auth_user = User.objects.create_user(username=f"p0_04_{role}")
            self.users[role] = auth_user
            self.principals[role] = UserKey.objects.create(user=auth_user)

        owner = self.users["owner"]
        self.project = Project.objects.create(
            user=self.principals["owner"],
            owner=owner,
            title="P0-04 permissions",
            format="series",
            annot="",
            desc="",
        )
        for role in ("owner", "admin", "editor", "viewer"):
            ProjectMember.objects.create(
                project=self.project,
                user=self.users[role],
                role=getattr(ProjectMemberRole, role.upper()),
                joined_at=timezone.now(),
            )

        self.character_service = CharacterService()
        self.character = self.character_service.create_character(
            self.principals["owner"],
            self.project,
            {"name": "Permission subject", "visual_style": "anime"},
        )
        self.client = APIClient()

    def tearDown(self):
        if self.previous_provider is None:
            os.environ.pop("CHARACTER_STUDIO_IMAGE_PROVIDER", None)
        else:
            os.environ["CHARACTER_STUDIO_IMAGE_PROVIDER"] = self.previous_provider
        super().tearDown()

    def assert_action(self, role, action, expected):
        principal = self.principals[role]
        if expected:
            project = get_project_for_action(principal, self.project.id, action)
            self.assertEqual(project.id, self.project.id)
            return
        with self.assertRaises(PermissionDeniedError):
            get_project_for_action(principal, self.project.id, action)

    def test_project_actions_follow_role_matrix(self):
        for role, expectations in self.ROLE_EXPECTATIONS.items():
            for action, expected in expectations.items():
                with self.subTest(role=role, action=action.value):
                    self.assert_action(role, action, expected)

    def test_character_service_boundaries_follow_role_matrix(self):
        getters = {
            Action.VIEW: self.character_service.get_viewable_character,
            Action.EDIT_CONTENT: self.character_service.get_editable_character,
            Action.RUN_GENERATION: self.character_service.get_generation_character,
        }
        for role, expectations in self.ROLE_EXPECTATIONS.items():
            for action, getter in getters.items():
                expected = expectations[action]
                with self.subTest(role=role, action=action.value):
                    args = (
                        self.principals[role],
                        self.project.id,
                        self.character.character_id,
                    )
                    if expected:
                        self.assertEqual(getter(*args), self.character)
                    else:
                        with self.assertRaises(PermissionDeniedError):
                            getter(*args)

    def test_character_read_endpoint_follows_view_matrix(self):
        url = (
            f"/api/projects/{self.project.id}/characters/"
            f"{self.character.character_id}"
        )
        for role, expectations in self.ROLE_EXPECTATIONS.items():
            with self.subTest(role=role):
                response = self.client.get(
                    url,
                    HTTP_X_USER_TOKEN=str(self.principals[role].key),
                )
                expected_status = 200 if expectations[Action.VIEW] else 403
                self.assertEqual(
                    response.status_code, expected_status, response.content,
                )

    def test_mutating_endpoints_follow_edit_content_matrix(self):
        revision_id = (
            self.character.revisions.order_by("created_at").first().revision_id
        )
        base = (
            f"/api/projects/{self.project.id}/characters/"
            f"{self.character.character_id}"
        )

        def outfit(token):
            return self.client.post(
                f"{base}/outfits",
                {"name": "Role test"},
                format="json",
                HTTP_X_USER_TOKEN=token,
            )

        def asset(token):
            upload = SimpleUploadedFile(
                "reference.png",
                b"\x89PNG\r\n\x1a\npermission-test",
                content_type="image/png",
            )
            return self.client.post(
                f"{base}/clothing-references",
                {"file": upload},
                format="multipart",
                HTTP_X_USER_TOKEN=token,
            )

        def revision(token):
            return self.client.post(
                f"{base}/revisions/{revision_id}/restore",
                {},
                format="json",
                HTTP_X_USER_TOKEN=token,
            )

        def references(token):
            return self.client.patch(
                f"{base}/references/checklist",
                {"appearance_stable": True},
                format="json",
                HTTP_X_USER_TOKEN=token,
            )

        def model3d(token):
            return self.client.put(
                f"{base}/model3d",
                {"params": {"torso": {"chestWidth": 0.2}}},
                format="json",
                HTTP_X_USER_TOKEN=token,
            )

        operations = {
            "outfit": (outfit, 201),
            "asset": (asset, 201),
            "revision": (revision, 201),
            "references": (references, 200),
            "model3d": (model3d, 200),
        }
        for role, expectations in self.ROLE_EXPECTATIONS.items():
            for operation_name, (operation, allowed_status) in operations.items():
                with self.subTest(role=role, operation=operation_name):
                    response = operation(str(self.principals[role].key))
                    expected_status = (
                        allowed_status
                        if expectations[Action.EDIT_CONTENT]
                        else 403
                    )
                    self.assertEqual(
                        response.status_code, expected_status, response.content,
                    )

    def test_generation_service_follows_run_generation_matrix(self):
        service = CharacterGenerationService()
        first_job = None
        for role, expectations in self.ROLE_EXPECTATIONS.items():
            args = (
                self.principals[role],
                self.project.id,
                self.character.character_id,
                {"variant_count": 1},
            )
            with self.subTest(role=role, operation="start_generation"):
                if expectations[Action.RUN_GENERATION]:
                    job = service.create_initial_variants(*args)
                    first_job = first_job or job
                    self.assertEqual(job.project_id, self.project.id)
                else:
                    job_count = CharacterGenerationJob.objects.count()
                    with self.assertRaises(PermissionDeniedError):
                        service.create_initial_variants(*args)
                    self.assertEqual(
                        CharacterGenerationJob.objects.count(), job_count,
                    )

        for role, expectations in self.ROLE_EXPECTATIONS.items():
            with self.subTest(role=role, operation="read_generation_job"):
                if expectations[Action.VIEW]:
                    job = service.get_generation_job(
                        self.principals[role], first_job.job_id,
                    )
                    self.assertEqual(job, first_job)
                else:
                    with self.assertRaises(PermissionDeniedError):
                        service.get_generation_job(
                            self.principals[role], first_job.job_id,
                        )
