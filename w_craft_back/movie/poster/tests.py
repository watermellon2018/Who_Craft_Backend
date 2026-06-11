"""Tests for the poster facade — covers the happy path through generate /
get / select / delete, plus the error paths that surface as PosterError
subclasses (and are translated to HTTP statuses by the view layer)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.poster import facade
from w_craft_back.movie.poster.errors import (
    PosterJobNotFound,
    PosterVariantDeleted,
    PosterVariantNotFound,
    ProjectAccessDenied,
    ProjectNotFound,
)
from w_craft_back.movie.poster.models import (
    PosterGenerationJob,
    PosterJobStatus,
    PosterVariant,
)
from w_craft_back.movie.project.dashboard_models import (
    ProjectMember,
    ProjectMemberRole,
)
from w_craft_back.movie.project.models import Project


def _make_user(username: str) -> User:
    user = User.objects.create_user(username=username, password='pw')
    UserKey.objects.create(user=user)
    return user


def _make_project(owner: User, title: str = 'My movie') -> Project:
    """Create a project with the right legacy + new ownership wiring.

    ``ProjectMember`` with role OWNER is what ``user_can_edit_project`` checks
    when the legacy ``Project.user`` FK is null.
    """
    uk = UserKey.objects.get(user=owner)
    project = Project.objects.create(
        owner=owner,
        user=uk,
        title=title,
        description='',
        format='',
        annot='',
        desc='',
    )
    ProjectMember.objects.create(
        project=project, user=owner, role=ProjectMemberRole.OWNER
    )
    return project


class PosterFacadeAccessTests(TestCase):
    """Anyone without project access gets an explicit PosterError, never silent."""

    def setUp(self):
        self.owner = _make_user('alice')
        self.outsider = _make_user('bob')
        self.project = _make_project(self.owner)

    def test_get_project_poster_404_for_missing_project(self):
        with self.assertRaises(ProjectNotFound):
            facade.get_project_poster(self.owner, project_id=999999)

    def test_get_project_poster_forbidden_for_outsider(self):
        with self.assertRaises(ProjectAccessDenied):
            facade.get_project_poster(self.outsider, project_id=self.project.id)

    def test_generate_poster_forbidden_for_outsider(self):
        with self.assertRaises(ProjectAccessDenied):
            facade.generate_poster(
                self.outsider,
                project_id=self.project.id,
                prompt='neon city',
                style='cinematic',
                format='vertical',
            )


class PosterFacadeHappyPathTests(TestCase):
    """The inline mock generator is wired up by default — this test follows the
    full create → get → select → delete cycle without a real worker."""

    def setUp(self):
        self.owner = _make_user('carol')
        self.project = _make_project(self.owner, title='Cyber dawn')

    def test_get_project_poster_creates_empty_poster_for_first_call(self):
        result = facade.get_project_poster(self.owner, project_id=self.project.id)
        self.assertIn('poster', result)
        self.assertEqual(result['recentVariants'], [])

    def test_generate_then_fetch_job(self):
        gen = facade.generate_poster(
            self.owner,
            project_id=self.project.id,
            prompt='neon city skyline',
            style='cinematic',
            format='vertical',
        )
        self.assertIn('jobId', gen)
        self.assertEqual(gen['status'], PosterJobStatus.COMPLETED)
        self.assertGreater(len(gen['variants']), 0)

        fetched = facade.get_poster_job(
            self.owner, project_id=self.project.id, job_id=gen['jobId']
        )
        self.assertEqual(fetched['job']['id'], gen['jobId'])

    def test_get_poster_job_404_for_unknown_id(self):
        with self.assertRaises(PosterJobNotFound):
            facade.get_poster_job(
                self.owner, project_id=self.project.id, job_id=999999
            )

    def test_select_variant_marks_it_chosen(self):
        gen = facade.generate_poster(
            self.owner,
            project_id=self.project.id,
            prompt='moody portrait',
            style='realism',
            format='square',
        )
        variant_id = gen['variants'][0]['id']
        result = facade.select_poster_variant(
            self.owner, project_id=self.project.id, variant_id=variant_id
        )
        self.assertEqual(result['selectedVariant']['id'], variant_id)
        self.assertEqual(result['poster']['selectedVariantId'], variant_id)

    def test_select_variant_404_for_unknown_variant(self):
        with self.assertRaises(PosterVariantNotFound):
            facade.select_poster_variant(
                self.owner, project_id=self.project.id, variant_id=999999
            )

    def test_select_variant_rejects_deleted_variant(self):
        gen = facade.generate_poster(
            self.owner,
            project_id=self.project.id,
            prompt='dark fantasy temple',
            style='dark_fantasy',
            format='horizontal',
        )
        variant_id = gen['variants'][0]['id']
        # Soft-delete then try to pick it.
        facade.delete_poster_variant(
            self.owner, project_id=self.project.id, variant_id=variant_id
        )
        with self.assertRaises(PosterVariantDeleted):
            facade.select_poster_variant(
                self.owner, project_id=self.project.id, variant_id=variant_id
            )

    def test_delete_variant_marks_it_deleted(self):
        gen = facade.generate_poster(
            self.owner,
            project_id=self.project.id,
            prompt='whatever',
            style='anime',
            format='vertical',
        )
        variant_id = gen['variants'][0]['id']
        facade.delete_poster_variant(
            self.owner, project_id=self.project.id, variant_id=variant_id
        )
        v = PosterVariant.objects.get(pk=variant_id)
        self.assertTrue(v.is_deleted)

    def test_delete_variant_is_idempotent(self):
        gen = facade.generate_poster(
            self.owner,
            project_id=self.project.id,
            prompt='whatever',
            style='anime',
            format='vertical',
        )
        variant_id = gen['variants'][0]['id']
        facade.delete_poster_variant(
            self.owner, project_id=self.project.id, variant_id=variant_id
        )
        # Calling again must not raise — facade short-circuits if already deleted.
        facade.delete_poster_variant(
            self.owner, project_id=self.project.id, variant_id=variant_id
        )
        self.assertEqual(
            PosterVariant.objects.filter(pk=variant_id, is_deleted=True).count(),
            1,
        )

    def test_get_poster_variants_respects_limit(self):
        # Generate three rounds, each adds a batch of variants (the mock
        # generator yields multiple per job).
        for prompt in ('a', 'b', 'c'):
            facade.generate_poster(
                self.owner,
                project_id=self.project.id,
                prompt=prompt,
                style='cinematic',
                format='vertical',
            )
        result = facade.get_poster_variants(
            self.owner, project_id=self.project.id, limit=2
        )
        self.assertEqual(len(result['variants']), 2)
