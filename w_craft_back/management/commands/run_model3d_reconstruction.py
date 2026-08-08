"""Run one queued per-character 3D reconstruction job."""

from django.core.management.base import BaseCommand, CommandError

from w_craft_back.character_studio.models import (
    CharacterGenerationJob,
    GenerationJobStatus,
)
from w_craft_back.character_studio.services.model3d_reconstruction_service import (
    run_reconstruction_job,
)


class Command(BaseCommand):
    help = "Run one queued Hunyuan3D character-head reconstruction job."

    def add_arguments(self, parser):
        parser.add_argument("--job-id", required=True)

    def handle(self, *args, **options):
        job_id = options["job_id"]
        asset = run_reconstruction_job(job_id)
        if asset is not None:
            self.stdout.write(self.style.SUCCESS(f"Created 3D asset {asset.asset_id}"))
            return
        status = CharacterGenerationJob.objects.filter(job_id=job_id).values_list(
            "status",
            flat=True,
        ).first()
        if status == GenerationJobStatus.PROCESSING:
            self.stdout.write("Reconstruction is already processing.")
            return
        raise CommandError(f"3D reconstruction {job_id} did not complete (status={status}).")
