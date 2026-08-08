from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project import policy, project_mutations
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project, ProjectStatus


def _user_with_token(username: str) -> tuple[User, str]:
    user = User.objects.create_user(username=username, password="pw")
    credential = UserKey.objects.create(user=user)
    return user, credential.key


def _project(owner: User, title: str) -> Project:
    credential = UserKey.objects.get(user=owner)
    project = Project.objects.create(
        user=credential,
        owner=owner,
        title=title,
        format="full-movie",
        annot="",
        desc="",
        status=ProjectStatus.IN_PROGRESS,
    )
    ProjectMember.objects.create(
        project=project,
        user=owner,
        role=ProjectMemberRole.OWNER,
    )
    return project


class ProjectSettingsPolicyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner, self.owner_token = _user_with_token("settings-owner")

    def test_project_patch_requires_edit_settings(self):
        cases = (
            (ProjectMemberRole.OWNER, 200),
            (ProjectMemberRole.ADMIN, 200),
            (ProjectMemberRole.EDITOR, 403),
            (ProjectMemberRole.VIEWER, 403),
            (None, 403),
        )

        for index, (role, expected_status) in enumerate(cases):
            with self.subTest(role=role):
                project = _project(self.owner, f"Project {index}")
                if role == ProjectMemberRole.OWNER:
                    actor, token = self.owner, self.owner_token
                else:
                    actor, token = _user_with_token(f"settings-actor-{index}")
                    if role is not None:
                        ProjectMember.objects.create(
                            project=project,
                            user=actor,
                            role=role,
                        )

                response = self.client.patch(
                    f"/api/projects/{project.id}/",
                    {
                        "title": f"Changed {index}",
                        "status": ProjectStatus.ARCHIVED,
                    },
                    format="json",
                    HTTP_X_USER_TOKEN=token,
                )

                self.assertEqual(
                    response.status_code,
                    expected_status,
                    response.content,
                )
                project.refresh_from_db()
                if expected_status == 200:
                    self.assertEqual(project.title, f"Changed {index}")
                    self.assertEqual(project.status, ProjectStatus.ARCHIVED)
                    self.assertIsNotNone(project.archived_at)
                else:
                    self.assertEqual(project.title, f"Project {index}")
                    self.assertEqual(project.status, ProjectStatus.IN_PROGRESS)
                    self.assertIsNone(project.archived_at)

    def test_character_attribution_uses_current_actor(self):
        project = _project(self.owner, "Actor attribution")
        editor, _ = _user_with_token("current-editor")
        ProjectMember.objects.create(
            project=project,
            user=editor,
            role=ProjectMemberRole.EDITOR,
        )

        character = project_mutations.create_project_character(
            actor=editor,
            action=policy.Action.EDIT_CONTENT,
            project_id=project.id,
            data={"name": "Shared character"},
        )

        self.assertEqual(character.project, project)
        self.assertEqual(character.user.user, editor)
