"""Run the local durable generation worker."""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand
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
from w_craft_back.movie.poster.lifecycle import recover_stale_poster_jobs
from w_craft_back.movie.poster.models import PosterGenerationJob, PosterJobStatus
from w_craft_back.movie.poster.worker import execute_poster_job




class Command(BaseCommand):
    help = "Poll the database and execute queued character, poster and 3D jobs."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=2.0)
        parser.add_argument("--batch-size", type=int, default=10)

    def handle(self, *args, **options):
        once = bool(options["once"])
        poll_interval = max(0.1, float(options["poll_interval"]))
        batch_size = max(1, min(int(options["batch_size"]), 1000))

        while True:
            if not once:
                close_old_connections()
            processed = self._poll_character_jobs(batch_size)
            processed += self._poll_poster_jobs(batch_size)
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
