from __future__ import annotations

from django.contrib.auth.models import User

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.dashboard_models import ProjectMember, ProjectMemberRole
from w_craft_back.movie.project.models import Project


def make_user(username: str) -> User:
    user = User.objects.create_user(username=username, password="pw")
    UserKey.objects.create(user=user)
    return user


def make_project(owner: User, title: str = "Music project") -> Project:
    project = Project.objects.create(
        owner=owner,
        user=UserKey.objects.get(user=owner),
        title=title,
        description="",
        format="full-movie",
        annot="",
        desc="",
    )
    ProjectMember.objects.create(
        project=project,
        user=owner,
        role=ProjectMemberRole.OWNER,
    )
    return project


def instrumental_brief(title: str = "Night cue") -> dict:
    return {
        "context": {"type": "project"},
        "content": {"mode": "instrumental"},
        "title": title,
        "purpose": "underscore",
        "genre": "cinematic",
        "moods": ["tense"],
        "durationSeconds": 3,
        "tempo": {"mode": "bpm", "bpm": 92},
        "energyCurve": "build",
        "instruments": ["low_strings", "analog_pulse"],
        "exclude": ["bright_brass"],
        "loopable": False,
        "textRefinement": "Leave an unresolved ending",
    }
