"""Seed a demo project that the /project-list/project page can render with
real backend data instead of frontend mocks.

Usage:
    python manage.py seed_project_dashboard
    python manage.py seed_project_dashboard --username alice
    python manage.py seed_project_dashboard --reset
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.models import (
    CharacterRole,
    CharacterStatus,
    StudioCharacter,
)
from w_craft_back.movie.project.dashboard_models import (
    ActivityType,
    Location,
    MusicTrack,
    ProjectActivity,
    ProjectMember,
    ProjectMemberRole,
    ProjectProgress,
    ProjectTag,
    Scene,
    SceneMusic,
)
from w_craft_back.movie.project.models import Project, ProjectStatus


PROJECT_TITLE = "Cyber City Dawn"


class Command(BaseCommand):
    help = "Create a demo Project with full dashboard data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            default=None,
            help="Username of the project owner. Defaults to first User in DB.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete the existing demo project (matched by title) before seeding.",
        )

    def handle(self, *args, **opts):
        user = self._resolve_user(opts.get("username"))
        if user is None:
            self.stderr.write("No user found — create one first.")
            return

        if opts.get("reset"):
            deleted, _ = Project.objects.filter(title=PROJECT_TITLE, owner=user).delete()
            if deleted:
                self.stdout.write(f"Deleted {deleted} existing demo records.")

        legacy_userkey, _ = UserKey.objects.get_or_create(user=user)

        project = Project.objects.filter(title=PROJECT_TITLE, owner=user).first()
        if project is None:
            project = Project.objects.create(
                user=legacy_userkey,
                owner=user,
                title=PROJECT_TITLE,
                description=(
                    "Детектив в мире будущего раскрывает заговор, способный изменить"
                    " судьбу всего человечества. Неон, дождь и тени корпораций."
                ),
                desc=(
                    "Детектив в мире будущего раскрывает заговор, способный изменить"
                    " судьбу всего человечества. Неон, дождь и тени корпораций."
                ),
                status=ProjectStatus.IN_PROGRESS,
                is_favorite=True,
                format="full-movie",
                annot="Demo project for dashboard.",
            )
            self.stdout.write(f"Created project #{project.id} '{project.title}'.")
        else:
            self.stdout.write(f"Reusing project #{project.id} '{project.title}'.")

        # Tags
        ProjectTag.objects.filter(project=project).delete()
        for name in ("Научная фантастика", "Киберпанк", "Драма"):
            ProjectTag.objects.create(project=project, name=name)

        # Owner membership
        ProjectMember.objects.update_or_create(
            project=project, user=user, defaults={"role": ProjectMemberRole.OWNER}
        )

        # Progress
        ProjectProgress.objects.update_or_create(
            project=project,
            defaults={
                "overall_progress": 58,
                "script_progress": 80,
                "visual_progress": 42,
                "audio_progress": 67,
                "postproduction_progress": 30,
            },
        )

        # Characters
        if not StudioCharacter.objects.filter(project=project).exists():
            char_specs = [
                ("Кай Синклер", CharacterRole.MAIN, "Детектив"),
                ("Лира Вэй", CharacterRole.SECONDARY, "Хакер"),
                ("Виктор Хейл", CharacterRole.ANTAGONIST, "Глава корпорации"),
                ("Мира Кейн", CharacterRole.SECONDARY, "Агент"),
            ]
            for name, role, desc in char_specs:
                StudioCharacter.objects.create(
                    project=project,
                    user=legacy_userkey,
                    name=name,
                    role=role,
                    short_description=desc,
                    status=CharacterStatus.ACTIVE,
                )

        # Locations + scenes
        if not Location.objects.filter(project=project).exists():
            night_market = Location.objects.create(
                project=project, name="Ночной рынок", is_created=True
            )
            Scene.objects.create(
                project=project,
                title="Ночной рынок",
                order=7,
                location=night_market,
                status="completed",
            )
            Scene.objects.create(
                project=project,
                title="Корпоративная башня",
                order=8,
                status="rendering",
            )

        # Music
        if not MusicTrack.objects.filter(project=project).exists():
            track1 = MusicTrack.objects.create(
                project=project,
                title="Neon Shadows",
                author="SynthWave Collective",
                duration_seconds=222,
                tags=["Напряжённый", "Киберпанк"],
            )
            track2 = MusicTrack.objects.create(
                project=project,
                title="Rain Over Tokyo",
                author="Akira Yamaoka",
                duration_seconds=258,
                tags=["Меланхоличный", "Атмосферный"],
            )
            for scene in Scene.objects.filter(project=project):
                SceneMusic.objects.get_or_create(scene=scene, track=track1)
            track2_scene = Scene.objects.filter(project=project).first()
            if track2_scene:
                SceneMusic.objects.get_or_create(scene=track2_scene, track=track2)

        # Activity timeline (created_at is auto_now_add, so order is creation order)
        if not ProjectActivity.objects.filter(project=project).exists():
            for kwargs in (
                {
                    "activity_type": ActivityType.CHARACTER_CREATED,
                    "title": "Виктор Хейл",
                    "description": "персонаж создан",
                },
                {
                    "activity_type": ActivityType.MUSIC_ADDED,
                    "title": "Добавлен трек",
                    "description": "Neon Shadows",
                },
                {
                    "activity_type": ActivityType.SCENE_RENDER_COMPLETED,
                    "title": "Сцена 07 — Ночной рынок",
                    "description": "рендер завершён",
                },
                {
                    "activity_type": ActivityType.CHARACTER_UPDATED,
                    "title": "Лира Вэй",
                    "description": "персонаж обновлён",
                },
            ):
                ProjectActivity.objects.create(project=project, user=user, **kwargs)

        self.stdout.write(self.style.SUCCESS(
            f"Seed complete. Project id={project.id}, owner={user.username}."
        ))

    def _resolve_user(self, username):
        if username:
            return User.objects.filter(username=username).first()
        return User.objects.order_by("id").first()
