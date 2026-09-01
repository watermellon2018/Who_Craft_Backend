"""Run the local durable generation worker."""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from w_craft_back.character_studio.models import (
    CharacterGenerationJob,
    GenerationJobStatus,
    GenerationJobType,
)
from w_craft_back.character_studio.services.generation_lifecycle import (
    recover_stale_jobs,
)
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.character_studio.services.model3d_reconstruction_service import (
    run_reconstruction_job,
)
from w_craft_back.movie.music.lifecycle import recover_stale_music_jobs
from w_craft_back.movie.music.worker import execute_next_music_job
from w_craft_back.movie.poster.lifecycle import recover_stale_poster_jobs
from w_craft_back.movie.poster.models import PosterGenerationJob, PosterJobStatus
from w_craft_back.movie.poster.worker import execute_poster_job
from w_craft_back.movie.reference_library.lifecycle import (
    recover_stale_reference_jobs,
)
from w_craft_back.movie.reference_library.worker import (
    execute_next_reference_job,
)
from w_craft_back.movie.sound_effects.lifecycle import (
    recover_stale_sound_effect_jobs,
)
from w_craft_back.movie.sound_effects.worker import (
    execute_next_sound_effect_job,
)
from w_craft_back.movie.storyboard.lifecycle import (
    recover_stale_storyboard_generations,
)
from w_craft_back.movie.storyboard.models import (
    StoryboardGenerationStatus,
    StoryboardKeyframeGeneration,
)
from w_craft_back.movie.storyboard.worker import execute_storyboard_generation
from w_craft_back.movie.storyboard.editor_frames import (
    execute_frame_job, recover_stale_frame_jobs,
)
from w_craft_back.movie.storyboard.shot_list_jobs import (
    execute_shot_list_job,
    recover_stale_shot_list_jobs,
)


class Command(BaseCommand):
    help = (
        "Poll selected durable character, poster, 3D, music, sound-effect, "
        "reference, and storyboard queues."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--queue",
            default="all",
            help=(
                "Comma-separated character,poster,music,sound_effect,reference,"
                "storyboard "
                "queues or all."
            ),
        )
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=2.0)
        parser.add_argument("--batch-size", type=int, default=10)

    def handle(self, *args, **options):
        once = bool(options["once"])
        poll_interval = max(0.1, float(options["poll_interval"]))
        batch_size = max(1, min(int(options["batch_size"]), 1000))
        raw_queues = str(options["queue"] or "all").lower().split(",")
        selected = {item.strip() for item in raw_queues if item.strip()}
        if "all" in selected:
            selected = {
                "character",
                "poster",
                "music",
                "sound_effect",
                "reference",
                "storyboard",
            }
        unknown = selected - {
            "character",
            "poster",
            "music",
            "sound_effect",
            "reference",
            "storyboard",
        }
        if unknown or not selected:
            raise CommandError(
                "Unknown generation queue(s): " + ", ".join(sorted(unknown))
            )

        while True:
            if not once:
                close_old_connections()
            processed = 0
            if "character" in selected:
                processed += self._poll_character_jobs(batch_size)
            if "poster" in selected:
                processed += self._poll_poster_jobs(batch_size)
            if "music" in selected:
                processed += self._poll_music_jobs(batch_size)
            if "sound_effect" in selected:
                processed += self._poll_sound_effect_jobs(batch_size)
            if "reference" in selected:
                processed += self._poll_reference_jobs(batch_size)
            if "storyboard" in selected:
                processed += self._poll_storyboard_jobs(batch_size)
            if not once:
                close_old_connections()
            if once:
                self.stdout.write(f"Processed {processed} generation job(s).")
                return
            if processed == 0:
                time.sleep(poll_interval)

    @staticmethod
    def _poll_character_jobs(batch_size: int) -> int:
        recover_stale_jobs(limit=batch_size)
        scan_limit = min(batch_size * 10, 1000)
        job_ids = list(
            CharacterGenerationJob.objects.filter(
                status=GenerationJobStatus.QUEUED,
            ).order_by("created_at").values_list("job_id", flat=True)[:scan_limit]
        )
        service = CharacterGenerationService()
        processed = 0
        for job_id in job_ids:
            if processed >= batch_size:
                break
            job_type = CharacterGenerationJob.objects.filter(
                job_id=job_id,
            ).values_list("job_type", flat=True).first()
            if job_type == GenerationJobType.MODEL3D_RECONSTRUCTION:
                run_reconstruction_job(job_id)
            else:
                service.execute_queued_job(job_id)
            status = CharacterGenerationJob.objects.filter(
                job_id=job_id,
            ).values_list("status", flat=True).first()
            if status != GenerationJobStatus.QUEUED:
                processed += 1
        return processed

    @staticmethod
    def _poll_poster_jobs(batch_size: int) -> int:
        recover_stale_poster_jobs(limit=batch_size)
        scan_limit = min(batch_size * 10, 1000)
        job_ids = list(
            PosterGenerationJob.objects.filter(
                status=PosterJobStatus.QUEUED,
            ).order_by("created_at").values_list("id", flat=True)[:scan_limit]
        )
        processed = 0
        for job_id in job_ids:
            if processed >= batch_size:
                break
            execute_poster_job(job_id)
            status = PosterGenerationJob.objects.filter(
                pk=job_id,
            ).values_list("status", flat=True).first()
            if status != PosterJobStatus.QUEUED:
                processed += 1
        return processed

    @staticmethod
    def _poll_music_jobs(batch_size: int) -> int:
        recover_stale_music_jobs(limit=batch_size)
        processed = 0
        while processed < batch_size:
            job = execute_next_music_job()
            if job is None:
                break
            processed += 1
        return processed

    @staticmethod
    def _poll_reference_jobs(batch_size: int) -> int:
        recover_stale_reference_jobs(limit=batch_size)
        processed = 0
        while processed < batch_size:
            job = execute_next_reference_job()
            if job is None:
                break
            processed += 1
        return processed

    @staticmethod
    def _poll_sound_effect_jobs(batch_size: int) -> int:
        recover_stale_sound_effect_jobs(limit=batch_size)
        processed = 0
        while processed < batch_size:
            job = execute_next_sound_effect_job()
            if job is None:
                break
            processed += 1
        return processed

    @staticmethod
    def _poll_storyboard_jobs(batch_size: int) -> int:
        recover_stale_frame_jobs(limit=batch_size)
        frame_processed = 0
        while frame_processed < batch_size:
            job = execute_frame_job()
            if job is None:
                break
            frame_processed += 1
        recover_stale_shot_list_jobs(limit=batch_size)
        text_processed = 0
        while text_processed < batch_size:
            job = execute_shot_list_job()
            if job is None:
                break
            text_processed += 1
        recover_stale_storyboard_generations(limit=batch_size)
        scan_limit = min(batch_size * 10, 1000)
        job_ids = list(
            StoryboardKeyframeGeneration.objects.filter(
                status=StoryboardGenerationStatus.QUEUED,
            ).order_by("created_at").values_list("id", flat=True)[:scan_limit]
        )
        processed = 0
        for job_id in job_ids:
            if processed >= batch_size:
                break
            execute_storyboard_generation(job_id)
            current_status = StoryboardKeyframeGeneration.objects.filter(
                pk=job_id,
            ).values_list("status", flat=True).first()
            if current_status != StoryboardGenerationStatus.QUEUED:
                processed += 1
        return processed + text_processed + frame_processed
