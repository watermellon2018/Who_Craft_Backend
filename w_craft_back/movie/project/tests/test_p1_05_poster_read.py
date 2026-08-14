from django.contrib.auth.models import User
from django.test import TestCase

from w_craft_back.movie.poster import facade
from w_craft_back.movie.poster.models import ProjectPoster, ProjectPosterStatus
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project


class PosterReadBoundaryTests(TestCase):
    def test_read_does_not_create_project_owned_poster(self):
        owner = User.objects.create_user(username="poster-read-owner")
        project = Project.objects.create(
            owner=owner,
            title="Read only",
            format="full-movie",
            annotation="",
            synopsis="",
        )
        ProjectMember.objects.create(
            project=project,
            user=owner,
            role=ProjectMemberRole.OWNER,
        )

        payload = facade.get_project_poster(owner, project.id)

        self.assertFalse(ProjectPoster.objects.filter(project=project).exists())
        self.assertIsNone(payload["poster"]["id"])
        self.assertEqual(payload["poster"]["status"], ProjectPosterStatus.EMPTY)
