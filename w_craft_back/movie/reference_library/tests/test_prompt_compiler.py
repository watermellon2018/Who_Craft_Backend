from django.test import SimpleTestCase

from w_craft_back.movie.reference_library.errors import ReferenceError
from w_craft_back.movie.reference_library.prompt_compiler import (
    compile_reference_prompt,
    normalize_brief,
)


class ReferencePromptCompilerTests(SimpleTestCase):
    def test_compilation_is_deterministic_and_preserves_identity_anchors(self):
        brief = {
            "schemaVersion": "reference_brief.v1",
            "materials": [" silver ", "red enamel", "SILVER"],
            "distinctiveFeatures": ["diagonal crack"],
            "aspectRatio": "1:1",
        }

        first = compile_reference_prompt(
            category="prop",
            description="Old red medallion",
            brief=brief,
        )
        second = compile_reference_prompt(
            category="prop",
            description="Old red medallion",
            brief=brief,
        )

        self.assertEqual(first, second)
        self.assertIn("diagonal crack", first.compiled_prompt)
        self.assertEqual(normalize_brief(brief)["materials"], ["silver", "red enamel"])

    def test_unknown_brief_fields_are_rejected(self):
        with self.assertRaises(ReferenceError) as raised:
            normalize_brief({"schemaVersion": "reference_brief.v1", "secret": "x"})

        self.assertEqual(raised.exception.code, "REFERENCE_INVALID_BRIEF")
