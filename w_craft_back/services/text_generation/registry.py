"""Curated structured-output text models and their ordered provider routes.

Keep product names here rather than in individual screens. Routes are LiteLLM
identifiers; their order is the default preference, not a quality or price rank.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TextModelSpec:
    key: str
    label: str
    routes: tuple[str, ...]


TEXT_MODELS = (
    TextModelSpec(
        key="google/gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        routes=(
            "gemini/gemini-2.5-flash",
            "openrouter/google/gemini-2.5-flash",
        ),
    ),
    TextModelSpec(
        key="qwen/qwen3-235b-a22b-2507",
        label="Qwen3 235B A22B 2507",
        routes=("openrouter/qwen/qwen3-235b-a22b-2507",),
    ),
    TextModelSpec(
        key="deepseek/deepseek-v3.2",
        label="DeepSeek V3.2",
        routes=("openrouter/deepseek/deepseek-v3.2",),
    ),
    TextModelSpec(
        key="openai/gpt-5.4-mini",
        label="GPT-5.4 mini",
        routes=("openrouter/openai/gpt-5.4-mini",),
    ),
)

DEFAULT_TEXT_MODEL_ROUTES = tuple(
    route for model in TEXT_MODELS for route in model.routes
)


def text_model_key(route: str) -> str:
    """Group only routes for the same namespaced model, retaining versions."""
    if route.startswith("gemini/"):
        return f"google/{route.removeprefix('gemini/')}"
    return route.removeprefix("openrouter/")


def text_model_label(route: str) -> str:
    """Return a model name without conflating it with the delivery provider."""
    key = text_model_key(route)
    for model in TEXT_MODELS:
        if model.key == key:
            return model.label
    return " ".join(part.capitalize() for part in key.split("/")[-1].split("-"))
