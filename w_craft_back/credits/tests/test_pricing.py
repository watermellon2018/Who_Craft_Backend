from decimal import Decimal

from django.test import SimpleTestCase

from w_craft_back.credits.pricing import estimate_for_spec
from w_craft_back.credits.services import GenerationPriceUnavailable
from w_craft_back.services.image_generation.registry import ModelSpec


def image_spec(pricing: dict) -> ModelSpec:
    return ModelSpec(
        key="openrouter-images:test/image-model",
        label="Test Image Model",
        backend="openrouter-images",
        model_id="test/image-model",
        mode="images",
        supports_generate=True,
        supports_edit=True,
        supports_reference=True,
        provider_pricing=pricing,
    )


class GenerationPricingTest(SimpleTestCase):
    def test_per_image_catalog_uses_conservative_provider_price_and_inputs(self):
        spec = image_spec({
            "source": "openrouter",
            "catalog": [
                {"billable": "output_image", "unit": "image",
                 "cost_usd": "0.03", "provider": "fast"},
                {"billable": "output_image", "unit": "image",
                 "cost_usd": "0.04", "provider": "quality"},
                {"billable": "input_image", "unit": "image",
                 "cost_usd": "0.003"},
            ],
        })

        estimate = estimate_for_spec(spec, reference_count=2)

        self.assertEqual(estimate.estimated_cost, Decimal("0.046000"))
        self.assertEqual(estimate.snapshot["referenceCount"], 2)
        self.assertEqual(estimate.snapshot["outputImageUnitCost"], "0.040000")
        self.assertEqual(estimate.snapshot["inputImageUnitCost"], "0.003")

    def test_token_and_megapixel_output_prices_are_not_treated_as_images(self):
        for unit in ("token", "megapixel"):
            spec = image_spec({
                "source": "openrouter",
                "catalog": [{
                    "billable": "output_image",
                    "unit": unit,
                    "cost_usd": "0.03",
                }],
            })

            with self.subTest(unit=unit), self.assertRaises(
                GenerationPriceUnavailable
            ):
                estimate_for_spec(spec)

    def test_mixed_output_units_do_not_underreserve_router_fallbacks(self):
        spec = image_spec({
            "source": "openrouter",
            "catalog": [
                {"billable": "output_image", "unit": "image",
                 "cost_usd": "0.04"},
                {"billable": "output_image", "unit": "token",
                 "cost_usd": "0.00003"},
            ],
        })

        with self.assertRaises(GenerationPriceUnavailable):
            estimate_for_spec(spec)

    def test_unbounded_reference_token_price_is_rejected_only_with_references(self):
        spec = image_spec({
            "source": "openrouter",
            "catalog": [
                {"billable": "output_image", "unit": "image",
                 "cost_usd": "0.04"},
                {"billable": "input_image", "unit": "token",
                 "cost_usd": "0.000008"},
            ],
        })

        self.assertEqual(
            estimate_for_spec(spec).estimated_cost,
            Decimal("0.040000"),
        )
        with self.assertRaises(GenerationPriceUnavailable):
            estimate_for_spec(spec, reference_count=1)
