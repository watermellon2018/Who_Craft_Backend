import os
from unittest.mock import patch

from django.test import SimpleTestCase

from w_craft_back.services.image_generation.errors import (
    CODE_UNAVAILABLE,
    ImageProviderError,
)
from w_craft_back.services.image_generation.routing import (
    RoutedImageProvider,
    build_routing_decision,
)


class GenerationRoutingTest(SimpleTestCase):
    @patch.dict(
        os.environ,
        {"GEMINI_API_KEY": "configured", "OPENROUTER_API_KEY": "configured"},
    )
    def test_economy_route_is_deterministic_and_reserves_bounded_fallback(self):
        decision = build_routing_decision(
            mode="economy",
            requested_model=None,
            operation="generate",
            variant_count=1,
            prompt_length=0,
            resolution="1K",
        )

        self.assertEqual(decision.mode, "economy")
        self.assertEqual(decision.primary.spec.key, "gemini-flash-image")
        self.assertEqual(len(decision.candidates), 2)
        self.assertEqual(
            decision.reservation_amount,
            sum(
                candidate.estimate.estimated_cost
                for candidate in decision.candidates
            ),
        )

    @patch.dict(
        os.environ,
        {"GEMINI_API_KEY": "configured", "OPENROUTER_API_KEY": "configured"},
    )
    def test_route_falls_back_only_for_retryable_provider_error(self):
        decision = build_routing_decision(
            mode="economy",
            requested_model=None,
            operation="generate",
        )
        providers = []

        class FakeProvider:
            def __init__(self, spec, *, fails):
                self.spec = spec
                self.name = spec.backend
                self.model_id = spec.model_id
                self.fails = fails

            def generate(self, *args, **kwargs):
                if self.fails:
                    raise ImageProviderError(
                        code=CODE_UNAVAILABLE,
                        message="temporary outage",
                        http_status=503,
                    )
                return [b"image"]

            def usage_snapshot(self):
                return {"costUsd": "0.001"}

        def factory(spec):
            provider = FakeProvider(spec, fails=not providers)
            providers.append(provider)
            return provider

        routed = RoutedImageProvider(decision.snapshot())
        with patch(
            "w_craft_back.services.image_generation.routing.provider_from_spec",
            side_effect=factory,
        ):
            result = routed.generate("prompt")

        self.assertEqual(result, [b"image"])
        usage = routed.usage_snapshot()
        self.assertEqual(len(usage["attempts"]), 2)
        self.assertEqual(usage["attempts"][0]["result"], "failed")
        self.assertEqual(usage["attempts"][1]["result"], "succeeded")
