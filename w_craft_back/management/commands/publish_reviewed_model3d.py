"""Review or persist a local, visually accepted character model candidate."""

import json
from pathlib import Path

from django.core.exceptions import (
    ObjectDoesNotExist, ValidationError as ModelValidationError,
)
from django.core.management.base import BaseCommand, CommandError

from w_craft_back.auth.models import UserKey
from w_craft_back.character_studio.services.errors import CharacterStudioError
from w_craft_back.character_studio.services.model3d_publish_service import (
    publish_reviewed_model3d,
)


class Command(BaseCommand):
    help = "Review head/hair GLBs; --apply publishes an immutable 3D revision."

    def add_arguments(self, parser):
        parser.add_argument("--character-id", required=True)
        parser.add_argument("--actor-id", required=True, type=int)
        parser.add_argument("--expected-revision", required=True, type=int)
        parser.add_argument("--expected-asset-id", required=True)
        parser.add_argument("--head", required=True, type=Path)
        parser.add_argument("--hair", required=True, type=Path)
        parser.add_argument("--head-sha256", required=True)
        parser.add_argument("--hair-sha256", required=True)
        parser.add_argument("--params", required=True, type=Path)
        parser.add_argument("--provenance", type=Path)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            params = json.loads(options["params"].read_text(encoding="utf-8-sig"))
            provenance = (
                json.loads(options["provenance"].read_text(encoding="utf-8-sig"))
                if options["provenance"] else None
            )
            result = publish_reviewed_model3d(
                actor=UserKey.objects.get(pk=options["actor_id"]),
                character_id=options["character_id"],
                expected_revision=options["expected_revision"],
                expected_asset_id=options["expected_asset_id"],
                head_path=options["head"], hair_path=options["hair"],
                head_sha256=options["head_sha256"], hair_sha256=options["hair_sha256"],
                params=params, provenance=provenance, apply=options["apply"],
            )
        except (CharacterStudioError, ObjectDoesNotExist, ModelValidationError,
                OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, indent=2, ensure_ascii=False))
