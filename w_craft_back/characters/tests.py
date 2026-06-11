"""Tests for the legacy ``characters`` app.

The view layer was removed when the legacy hero editor was retired (see
git history). The ``Character`` model itself stays because the
``display_tree.ItemFolder.hero`` FK still points at it (and live migrations
own the table). These tests lock in the validator on ``birth_date`` so a
future change to the format doesn't silently break the existing rows.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from w_craft_back.auth.models import UserKey
from w_craft_back.characters.creating.models import Character
from w_craft_back.movie.project.models import Project


def _make_project(username: str) -> Project:
    user = User.objects.create_user(username=username, password='pw')
    uk = UserKey.objects.create(user=user)
    return Project.objects.create(
        owner=user, user=uk, title='Project',
        description='', format='', annot='', desc='',
    )


class CharacterModelValidationTests(TestCase):
    """Validators on the legacy Character model — birth_date must be dd.mm.yyyy."""

    def setUp(self):
        self.project = _make_project('carol')

    def _full_character(self, **overrides):
        # Model has several non-blank text fields with empty default — passing
        # all of them keeps full_clean focused on the validator under test.
        kwargs = dict(
            project=self.project,
            first_name='X',
            last_name='Y',
            middle_name='Z',
            birth_place='Earth',
            birth_date='15.05.2026',
        )
        kwargs.update(overrides)
        return Character(**kwargs)

    def test_valid_birth_date_passes(self):
        # Should not raise.
        self._full_character().full_clean()

    def test_malformed_birth_date_raises(self):
        with self.assertRaises(ValidationError):
            self._full_character(birth_date='2026/05/15').full_clean()
