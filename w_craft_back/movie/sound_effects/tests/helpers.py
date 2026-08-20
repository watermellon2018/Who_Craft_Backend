from django.contrib.auth.models import User

from w_craft_back.auth.models import UserKey
from w_craft_back.movie.project.dashboard_models import ProjectMember, ProjectMemberRole
from w_craft_back.movie.project.models import Project


def make_user(username: str) -> User:
    user = User.objects.create_user(username=username, password="pw")
    UserKey.objects.create(user=user)
    return user


def make_project(owner: User) -> Project:
    project = Project.objects.create(
        owner=owner,
        title="Sound effects project",
        summary="",
        format="feature_film",
        annotation="",
        synopsis="",
    )
    ProjectMember.objects.create(
        project=project,
        user=owner,
        role=ProjectMemberRole.OWNER,
    )
    return project


def mp3_bytes(frame_count: int = 20) -> bytes:
    header = b"\xff\xfb\x90\x00"
    frame_length = 417
    return b"".join(header + bytes(frame_length - 4) for _ in range(frame_count))


def request_payload(**overrides) -> dict:
    payload = {
        "modelKey": "elevenlabs-sound-effects-v2",
        "prompt": "Heavy wooden door slams in a stone corridor",
        "durationSeconds": 2.5,
        "loop": False,
        "promptInfluence": 0.4,
    }
    payload.update(overrides)
    return payload
