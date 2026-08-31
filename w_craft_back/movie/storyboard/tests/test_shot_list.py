from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.throttling import UserRateThrottle

from w_craft_back.movie.storyboard.errors import StoryboardError
from w_craft_back.movie.storyboard.shot_list import (
    AIShotListService,
    LiteLLMShotListProvider,
)
from w_craft_back.movie.storyboard.views import (
    SceneStoryboardShotListView,
    StoryboardShotListRateThrottle,
)
from w_craft_back.services.text_generation.registry import DEFAULT_TEXT_MODEL_ROUTES


CONTEXT = {
    "scene": {"title": "Причал", "text": "Энгри Дог встречает Анчоуса."},
    "characters": [{"id": "dog", "name": "Энгри Дог"}],
    "locations": [{"id": "pier", "title": "Причал"}],
    "visualAssets": [],
}


@override_settings(
    GEMINI_API_KEY="test-gemini-key",
    OPENROUTER_API_KEY="",
    STORYBOARD_SHOT_LIST_MODEL="gemini/gemini-2.5-flash",
    STORYBOARD_SHOT_LIST_MODELS=",".join(DEFAULT_TEXT_MODEL_ROUTES),
)
class ShotListModelTests(SimpleTestCase):
    def test_options_report_availability_context_and_estimated_cost(self):
        litellm = SimpleNamespace(
            model_cost={
                "gemini/gemini-2.5-flash": {
                    "input_cost_per_token": 0.0000001,
                    "output_cost_per_token": 0.0000004,
                }
            },
            token_counter=lambda **kwargs: 100,
        )

        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=litellm,
        ):
            result = AIShotListService.options(context=CONTEXT, max_shots=16)

        self.assertEqual(result["defaultModel"], "gemini/gemini-2.5-flash")
        self.assertEqual(result["context"]["characters"], ["Энгри Дог"])
        self.assertEqual(result["models"][0]["estimatedInputTokens"], 100)
        self.assertEqual(result["models"][0]["estimatedOutputTokens"], 2880)
        self.assertEqual(result["models"][0]["estimatedCostUsd"], "0.001162")
        self.assertTrue(result["models"][0]["available"])
        self.assertEqual(
            [option["label"] for option in result["models"]],
            ["Gemini 2.5 Flash", "Qwen3 235B A22B 2507",
             "DeepSeek V3.2", "GPT-5.4 mini"],
        )
        self.assertEqual(result["models"][0]["provider"], "Google Gemini")
        self.assertFalse(result["models"][1]["available"])
        self.assertEqual(
            result["models"][1]["unavailableReason"],
            "credentialMissing",
        )

    @override_settings(GEMINI_API_KEY="", OPENROUTER_API_KEY="test-openrouter")
    def test_openrouter_only_selects_one_route_per_model(self):
        litellm = SimpleNamespace(
            model_cost={
                "gemini/gemini-2.5-flash": {
                    "input_cost_per_token": 0.1,
                    "output_cost_per_token": 0.4,
                },
                "openrouter/google/gemini-2.5-flash": {
                    "input_cost_per_token": 0.0000002,
                    "output_cost_per_token": 0.0000008,
                },
            },
            token_counter=lambda **kwargs: 100,
        )
        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=litellm,
        ):
            result = AIShotListService.options(context=CONTEXT, max_shots=16)
            default_provider = LiteLLMShotListProvider()

        self.assertEqual(len(result["models"]), 4)
        self.assertEqual(
            result["defaultModel"], "openrouter/google/gemini-2.5-flash"
        )
        self.assertEqual(default_provider.model, result["defaultModel"])
        self.assertEqual(result["models"][0]["estimatedCostUsd"], "0.002324")
        for option in result["models"]:
            self.assertTrue(option["available"])
            self.assertEqual(option["provider"], "OpenRouter")

    @override_settings(
        OPENROUTER_API_KEY="test-openrouter",
        STORYBOARD_SHOT_LIST_MODEL="openrouter/google/gemini-2.5-flash",
    )
    def test_explicit_default_route_takes_priority_when_both_are_configured(self):
        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=SimpleNamespace(model_cost={}),
        ):
            result = AIShotListService.options(context=CONTEXT, max_shots=16)
        self.assertEqual(len(result["models"]), 4)
        self.assertEqual(result["models"][0]["id"], result["defaultModel"])
        self.assertEqual(result["models"][0]["provider"], "OpenRouter")

    @override_settings(
        GEMINI_API_KEY="", OPENROUTER_API_KEY="test-openrouter",
        STORYBOARD_SHOT_LIST_MODELS="gemini/gemini-2.5-flash",
    )
    def test_does_not_enable_routes_outside_explicit_allowlist(self):
        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=SimpleNamespace(model_cost={}),
        ):
            result = AIShotListService.options(context=CONTEXT, max_shots=16)
        self.assertEqual(len(result["models"]), 1)
        self.assertFalse(result["models"][0]["available"])

    @override_settings(GEMINI_API_KEY="", OPENROUTER_API_KEY="test-openrouter")
    def test_missing_route_price_does_not_use_direct_provider_price(self):
        litellm = SimpleNamespace(model_cost={
            "gemini/gemini-2.5-flash": {
                "input_cost_per_token": 0.0000001,
                "output_cost_per_token": 0.0000004,
            },
        })
        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=litellm,
        ):
            result = AIShotListService.options(context=CONTEXT, max_shots=16)
        self.assertIsNone(result["models"][0]["estimatedCostUsd"])

    @override_settings(GEMINI_API_KEY="", OPENROUTER_API_KEY="test-openrouter")
    def test_selected_route_becoming_unavailable_is_not_silently_replaced(self):
        completion = Mock()
        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=SimpleNamespace(completion=completion),
        ), self.assertRaises(StoryboardError) as captured:
            LiteLLMShotListProvider(model="gemini/gemini-2.5-flash")
        self.assertEqual(captured.exception.code, "STORYBOARD_AI_NOT_CONFIGURED")
        completion.assert_not_called()

    @override_settings(OPENROUTER_API_KEY="test-openrouter")
    def test_new_models_preserve_route_and_require_schema_support(self):
        for model_id in (
            "openrouter/qwen/qwen3-235b-a22b-2507",
            "openrouter/deepseek/deepseek-v3.2",
            "openrouter/openai/gpt-5.4-mini",
        ):
            with self.subTest(model=model_id):
                completion = Mock(return_value=SimpleNamespace(choices=[
                    SimpleNamespace(message=SimpleNamespace(content='{"shots": []}')),
                ]))
                with patch(
                    "w_craft_back.movie.storyboard.shot_list._load_litellm",
                    return_value=SimpleNamespace(completion=completion),
                ):
                    provider = LiteLLMShotListProvider(model=model_id)
                    provider.suggest(prompt="scene", schema={"type": "object"})
                request = completion.call_args.kwargs
                self.assertEqual(request["model"], model_id)
                self.assertEqual(request["extra_body"]["provider"], {
                    "require_parameters": True, "allow_fallbacks": False,
                })
                self.assertEqual(request["response_format"]["type"], "json_schema")
                self.assertTrue(request["response_format"]["json_schema"]["strict"])
                self.assertEqual(request["num_retries"], 0)
                completion.assert_called_once()

    @override_settings(OPENROUTER_API_KEY="test-openrouter")
    def test_provider_failure_does_not_retry_another_route(self):
        completion = Mock(side_effect=TimeoutError("provider timeout"))
        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=SimpleNamespace(completion=completion),
        ):
            provider = LiteLLMShotListProvider(
                model="openrouter/google/gemini-2.5-flash"
            )
            with self.assertRaises(StoryboardError) as captured:
                provider.suggest(prompt="scene", schema={"type": "object"})
        self.assertEqual(captured.exception.code, "STORYBOARD_AI_TIMEOUT")
        completion.assert_called_once()
        self.assertEqual(
            completion.call_args.kwargs["model"],
            "openrouter/google/gemini-2.5-flash",
        )

    def test_selected_allowlisted_model_is_sent_to_litellm(self):
        completion = Mock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"shots": []}')
                    )
                ]
            )
        )
        litellm = SimpleNamespace(completion=completion)

        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=litellm,
        ):
            provider = LiteLLMShotListProvider(
                model="gemini/gemini-2.5-flash"
            )
            provider.suggest(prompt="scene", schema={"type": "object"})

        self.assertEqual(
            completion.call_args.kwargs["model"],
            "gemini/gemini-2.5-flash",
        )

    def test_arbitrary_model_is_rejected_before_provider_call(self):
        with self.assertRaises(StoryboardError) as captured:
            LiteLLMShotListProvider(model="openai/unapproved-model")

        self.assertEqual(
            captured.exception.code,
            "STORYBOARD_AI_MODEL_UNAVAILABLE",
        )

    @override_settings(
        STORYBOARD_SHOT_LIST_MODELS="openai/gpt-4o-mini",
    )
    def test_allowlisted_unsupported_provider_is_reported_unavailable(self):
        litellm = SimpleNamespace(model_cost={})

        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=litellm,
        ):
            result = AIShotListService.options(context=CONTEXT, max_shots=16)

        openai_option = next(
            option
            for option in result["models"]
            if option["id"] == "openai/gpt-4o-mini"
        )
        self.assertFalse(openai_option["available"])
        self.assertEqual(
            openai_option["unavailableReason"],
            "unsupportedProvider",
        )

    def test_missing_litellm_is_reported_as_configuration_error(self):
        with patch(
            "w_craft_back.movie.storyboard.shot_list._load_litellm",
            return_value=None,
        ), self.assertRaises(StoryboardError) as captured:
            LiteLLMShotListProvider(model="gemini/gemini-2.5-flash")

        self.assertEqual(captured.exception.code, "STORYBOARD_AI_NOT_CONFIGURED")
        self.assertEqual(captured.exception.http_status, 503)

    @override_settings(
        REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": {}},
        STORYBOARD_SHOT_LIST_THROTTLE_RATE="7/min",
    )
    def test_endpoint_throttle_does_not_require_a_drf_scope_entry(self):
        self.assertEqual(StoryboardShotListRateThrottle().get_rate(), "7/min")

    def test_only_generation_uses_the_stricter_endpoint_throttle(self):
        view = SceneStoryboardShotListView()
        view.request = SimpleNamespace(method="GET")
        self.assertIsInstance(view.get_throttles()[0], UserRateThrottle)
        self.assertNotIsInstance(
            view.get_throttles()[0],
            StoryboardShotListRateThrottle,
        )

        view.request = SimpleNamespace(method="POST")
        self.assertIsInstance(
            view.get_throttles()[0],
            StoryboardShotListRateThrottle,
        )
