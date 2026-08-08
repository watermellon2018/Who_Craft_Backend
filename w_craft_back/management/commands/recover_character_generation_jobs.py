from django.core.management.base import BaseCommand

from w_craft_back.character_studio.services.generation_lifecycle import (
    recover_stale_jobs,
)
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)


class Command(BaseCommand):
    help = (
        "Recover expired character image generation leases. Jobs whose "
        "provider call may have started are failed instead of retried."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum number of expired leases to inspect.",
        )

    def handle(self, *args, **options):
        result = recover_stale_jobs(limit=options["limit"])
        service = CharacterGenerationService()
        for job_id in result["requeued"]:
            service.execute_queued_job(job_id)
        self.stdout.write(
            self.style.SUCCESS(
                "Recovered character generation jobs: "
                f"requeued={len(result['requeued'])}, "
                f"executed={len(result['requeued'])}, "
                f"failed={len(result['failed'])}."
            )
        )
