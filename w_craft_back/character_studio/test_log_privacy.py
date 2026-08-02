from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from w_craft_back.character_studio.models import GenerationJobStatus
from w_craft_back.character_studio.services.generation_service import (
    CharacterGenerationService,
)
from w_craft_back.character_studio.services.prompt_compiler import (
    CharacterPromptCompiler,
)
from w_craft_back.character_studio.services.providers import MockProvider
from w_craft_back.character_studio.services.serialization import job_dict
from w_craft_back.character_studio.tests import CharacterStudioTestCase
from w_craft_back.services.image_generation.errors import (
    CODE_ERROR,
    map_to_provider_error,
)


PROVIDER_FACTORY = (
    "w_craft_back.character_studio.services.generation_service."
    "get_image_provider"
)
SECRET_DESCRIPTION = "log01-private-description-8f4c2e91"


class EchoingFailureProvider(MockProvider):
    model_name = "privacy-test-model"
    model_version = "privacy-test-v1"

    def generate_character_variants(
        self,
        job,
        compiled_prompt,
        variant_count,
    ):
        del job, variant_count
        raise RuntimeError(
            f"provider echoed fragment: {compiled_prompt['positive_prompt']}"
        )


class PromptLoggingPrivacyTests(SimpleTestCase):
    @override_settings(GENERATION_LOG_RAW_PROMPTS=False)
    def test_info_log_contains_hash_and_length_but_not_raw_prompt(self):
        compiler = CharacterPromptCompiler()

        with patch(
            "w_craft_back.character_studio.services.prompt_compiler.logger.info"
        ) as log_info, patch(
            "w_craft_back.character_studio.services.prompt_compiler.logger.debug"
        ) as log_debug:
            compiled = compiler.compile(
                character=None,
                appearance=None,
                outfit=None,
                text_refinement=SECRET_DESCRIPTION,
            )

        prompt = compiled["positive_prompt"]
        metadata = log_info.call_args.kwargs["extra"]
        self.assertEqual(metadata["prompt_len"], len(prompt))
        self.assertEqual(
            metadata["prompt_hash"],
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn(SECRET_DESCRIPTION, repr(log_info.call_args))
        log_debug.assert_not_called()

    def test_unmapped_provider_error_discards_raw_fragment(self):
        raw_error = RuntimeError(f"provider echoed {SECRET_DESCRIPTION}")

        with self.assertLogs(
            "w_craft_back.services.image_generation.errors",
            level="ERROR",
        ) as captured:
            mapped = map_to_provider_error(raw_error)

        self.assertEqual(mapped.code, CODE_ERROR)
        self.assertIsNone(mapped.provider_body)
        self.assertNotIn(SECRET_DESCRIPTION, "\n".join(captured.output))
        self.assertNotIn(SECRET_DESCRIPTION, repr(mapped.__dict__))


@override_settings(GENERATION_LOG_RAW_PROMPTS=False)
class GenerationFailurePrivacyTests(CharacterStudioTestCase):
    def test_provider_echo_is_absent_from_logs_and_public_job_error(self):
        character = self.create_character()
        character.short_description = SECRET_DESCRIPTION
        character.save(update_fields=["short_description"])

        with self.assertLogs(
            "w_craft_back.character_studio",
            level="INFO",
        ) as captured, patch(
            PROVIDER_FACTORY,
            return_value=EchoingFailureProvider(),
        ):
            job = CharacterGenerationService().create_initial_variants(
                self.user_key,
                self.project.id,
                character.character_id,
                {"variant_count": 1},
            )

        job.refresh_from_db()
        public_payload = job_dict(job)
        serialized_public_payload = json.dumps(public_payload, ensure_ascii=False)

        self.assertEqual(job.status, GenerationJobStatus.FAILED)
        self.assertEqual(job.error_code, "GENERATION_FAILED")
        self.assertEqual(job.error_message, "Generation failed. Try again.")
        self.assertNotIn(SECRET_DESCRIPTION, "\n".join(captured.output))
        self.assertNotIn(SECRET_DESCRIPTION, serialized_public_payload)
