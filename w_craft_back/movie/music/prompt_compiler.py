"""Deterministic MusicBrief normalization and provider-neutral compilation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


PURPOSE_PHRASES = {
    "underscore": "narrative underscore",
    "ambience": "environmental ambience",
    "transition": "scene transition",
    "stinger": "short dramatic stinger",
    "song": "full song",
}
GENRE_PHRASES = {
    "cinematic": "cinematic score",
    "cinematic_pop": "cinematic pop",
    "ambient": "ambient soundscape",
    "electronic": "electronic music",
    "orchestral": "orchestral score",
    "acoustic": "acoustic arrangement",
    "experimental": "experimental composition",
    "pop": "pop arrangement",
}
MOOD_PHRASES = {
    "tense": "tense",
    "mysterious": "mysterious",
    "hopeful": "hopeful",
    "melancholic": "melancholic",
    "warm": "warm",
    "dark": "dark",
    "triumphant": "triumphant",
    "romantic": "romantic",
}
INSTRUMENT_PHRASES = {
    "low_strings": "low strings",
    "full_strings": "full string section",
    "piano": "piano",
    "acoustic_guitar": "acoustic guitar",
    "electric_guitar": "electric guitar",
    "analog_pulse": "analog pulse",
    "synth_pad": "synth pad",
    "drums": "drums",
    "percussion": "percussion",
    "brass": "brass",
    "woodwinds": "woodwinds",
    "choir": "choir",
}
ENERGY_PHRASES = {
    "steady": "steady energy",
    "build": "gradually building energy",
    "peak": "energy peaking near the climax",
    "fade": "energy fading toward the end",
}
TEMPO_PHRASES = {
    "auto": "tempo chosen for the brief",
    "slow": "slow tempo",
    "medium": "medium tempo",
    "fast": "fast tempo",
}
SECTION_TYPES = {"verse", "chorus", "bridge", "outro"}
MAX_REFINEMENT_CHARS = 1000
MAX_SCENE_SUMMARY_CHARS = 500
MAX_PROMPT_CHARS = 4000
MAX_MUSIC_SEED = 4_294_967_295


class MusicBriefError(ValueError):
    """Raised when a brief cannot be normalized safely."""

    code = "MUSIC_VALIDATION_ERROR"
    http_status = 400


def _required_text(value: Any, field: str, *, limit: int = 255) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise MusicBriefError(f"{field} must contain 1-{limit} characters.")
    return text


def _choice(value: Any, field: str, choices: Mapping[str, str]) -> str:
    normalized = str(value or "").strip()
    if normalized not in choices:
        raise MusicBriefError(f"Unsupported {field}.")
    return normalized


def _unique_choices(
    value: Any,
    field: str,
    choices: Mapping[str, str],
    *,
    minimum: int = 0,
    maximum: int,
) -> list[str]:
    if value is None:
        items: list[Any] = []
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise MusicBriefError(f"{field} must be a list.")
    normalized: list[str] = []
    for raw in items:
        item = str(raw or "").strip()
        if item not in choices:
            raise MusicBriefError(f"Unsupported value in {field}.")
        if item not in normalized:
            normalized.append(item)
    if not minimum <= len(normalized) <= maximum:
        raise MusicBriefError(
            f"{field} must contain between {minimum} and {maximum} unique values."
        )
    return normalized


def normalize_music_brief(brief: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable camelCase normal form while preserving lyrics verbatim."""

    if not isinstance(brief, Mapping):
        raise MusicBriefError("brief must be an object.")
    source = deepcopy(dict(brief))
    content = source.get("content")
    if not isinstance(content, Mapping):
        raise MusicBriefError("content must be an object.")
    mode = str(content.get("mode") or "").strip()
    if mode not in {"instrumental", "song"}:
        raise MusicBriefError("content.mode must be instrumental or song.")

    normalized_content: dict[str, Any] = {"mode": mode}
    if mode == "song":
        language = str(content.get("lyricsLanguage") or "").strip()
        if language not in {"ru", "en"}:
            raise MusicBriefError("Unsupported lyrics language.")
        raw_sections = content.get("sections")
        if not isinstance(raw_sections, list) or not 1 <= len(raw_sections) <= 30:
            raise MusicBriefError("Songs require 1-30 ordered lyrics sections.")
        sections: list[dict[str, str]] = []
        lyrics_chars = 0
        for raw_section in raw_sections:
            if not isinstance(raw_section, Mapping):
                raise MusicBriefError("Each lyrics section must be an object.")
            section_type = str(raw_section.get("type") or "").strip()
            if section_type not in SECTION_TYPES:
                raise MusicBriefError("Unsupported lyrics section type.")
            text = raw_section.get("text")
            if not isinstance(text, str) or not text.strip():
                raise MusicBriefError("Lyrics section text cannot be empty.")
            label = raw_section.get("label", "")
            if not isinstance(label, str) or len(label) > 100:
                raise MusicBriefError("Lyrics section label is invalid.")
            lyrics_chars += len(text)
            sections.append({"type": section_type, "label": label, "text": text})
        if lyrics_chars > 12000:
            raise MusicBriefError("Lyrics exceed the configured character limit.")
        vocal_style = content.get("vocalStyle") or {}
        if not isinstance(vocal_style, Mapping):
            raise MusicBriefError("vocalStyle must be an object.")
        normalized_vocal = {
            key: str(vocal_style[key]).strip()
            for key in ("timbre", "delivery", "density")
            if vocal_style.get(key) not in (None, "")
        }
        normalized_content.update(
            {
                "lyricsLanguage": language,
                "vocalStyle": normalized_vocal,
                "sections": sections,
            }
        )
    elif content.get("sections") or content.get("lyricsLanguage"):
        raise MusicBriefError("Instrumental briefs cannot contain lyrics.")

    duration = source.get("durationSeconds")
    if isinstance(duration, bool):
        raise MusicBriefError("durationSeconds must be an integer.")
    try:
        duration_seconds = int(duration)
    except (TypeError, ValueError) as exc:
        raise MusicBriefError("durationSeconds must be an integer.") from exc
    if not 3 <= duration_seconds <= 300:
        raise MusicBriefError("durationSeconds must be between 3 and 300.")

    tempo = source.get("tempo") or {"mode": "auto"}
    if not isinstance(tempo, Mapping):
        raise MusicBriefError("tempo must be an object.")
    tempo_mode = str(tempo.get("mode") or "").strip()
    if tempo_mode not in {"auto", "slow", "medium", "fast", "bpm"}:
        raise MusicBriefError("Unsupported tempo mode.")
    normalized_tempo: dict[str, Any] = {"mode": tempo_mode}
    if tempo_mode == "bpm":
        try:
            bpm = int(tempo.get("bpm"))
        except (TypeError, ValueError) as exc:
            raise MusicBriefError("tempo.bpm must be an integer.") from exc
        if not 40 <= bpm <= 220:
            raise MusicBriefError("tempo.bpm must be between 40 and 220.")
        normalized_tempo["bpm"] = bpm
    elif tempo.get("bpm") not in (None, ""):
        raise MusicBriefError("tempo.bpm is only valid for bpm mode.")

    refinement = source.get("textRefinement", "")
    if not isinstance(refinement, str) or len(refinement) > MAX_REFINEMENT_CHARS:
        raise MusicBriefError("textRefinement exceeds the character limit.")
    context = source.get("context") or {"type": "project"}
    if not isinstance(context, Mapping):
        raise MusicBriefError("context must be an object.")
    context_type = str(context.get("type") or "project").strip()
    normalized_context: dict[str, Any] = {"type": context_type}
    if context_type == "scene":
        try:
            normalized_context["sceneId"] = int(context.get("sceneId"))
        except (TypeError, ValueError) as exc:
            raise MusicBriefError("context.sceneId must be an integer.") from exc
    elif context_type != "project":
        raise MusicBriefError("Unsupported context type.")

    seed = source.get("seed")
    normalized_seed: int | None = None
    if seed is not None:
        if isinstance(seed, bool):
            raise MusicBriefError("seed must be an integer.")
        try:
            normalized_seed = int(seed)
        except (TypeError, ValueError) as exc:
            raise MusicBriefError("seed must be an integer.") from exc
        if not 0 <= normalized_seed <= MAX_MUSIC_SEED:
            raise MusicBriefError(
                f"seed must be between 0 and {MAX_MUSIC_SEED}."
            )

    exclude_choices = {**INSTRUMENT_PHRASES, "bright_brass": "bright brass"}
    normalized = {
        "context": normalized_context,
        "content": normalized_content,
        "title": _required_text(source.get("title"), "title"),
        "purpose": _choice(source.get("purpose"), "purpose", PURPOSE_PHRASES),
        "genre": _choice(source.get("genre"), "genre", GENRE_PHRASES),
        "moods": _unique_choices(
            source.get("moods"), "moods", MOOD_PHRASES, minimum=1, maximum=3
        ),
        "durationSeconds": duration_seconds,
        "tempo": normalized_tempo,
        "energyCurve": _choice(
            source.get("energyCurve"), "energyCurve", ENERGY_PHRASES
        ),
        "instruments": _unique_choices(
            source.get("instruments", []),
            "instruments",
            INSTRUMENT_PHRASES,
            maximum=6,
        ),
        "exclude": _unique_choices(
            source.get("exclude", []),
            "exclude",
            exclude_choices,
            maximum=6,
        ),
        "loopable": bool(source.get("loopable", False)),
        "textRefinement": refinement,
    }
    if normalized_seed is not None:
        normalized["seed"] = normalized_seed
    return normalized


def compile_music_prompt(
    brief: Mapping[str, Any],
    *,
    scene_context: Mapping[str, Any] | None = None,
    reference_asset_id: object | None = None,
    variant_count: int = 2,
) -> dict[str, Any]:
    """Compile a normalized brief into a deterministic provider-neutral request."""

    normalized = normalize_music_brief(brief)
    if variant_count not in (1, 2):
        raise MusicBriefError("variantCount must be 1 or 2.")
    context_snapshot: dict[str, Any] | None = None
    if normalized["context"]["type"] == "scene":
        if not scene_context:
            raise MusicBriefError("Scene context could not be resolved.")
        summary = str(scene_context.get("summary") or "").strip()
        context_snapshot = {
            "sceneId": int(scene_context["sceneId"]),
            "title": str(scene_context.get("title") or "")[:255],
            "durationSeconds": int(scene_context.get("durationSeconds") or 0),
            "mood": str(scene_context.get("mood") or "")[:100],
            "sceneType": str(scene_context.get("sceneType") or "")[:32],
            "summary": summary[:MAX_SCENE_SUMMARY_CHARS],
        }

    tempo = normalized["tempo"]
    tempo_phrase = (
        f"exactly {tempo['bpm']} BPM"
        if tempo["mode"] == "bpm"
        else TEMPO_PHRASES[tempo["mode"]]
    )
    parts = [
        PURPOSE_PHRASES[normalized["purpose"]],
        GENRE_PHRASES[normalized["genre"]],
        ", ".join(MOOD_PHRASES[item] for item in normalized["moods"]),
        tempo_phrase,
        ENERGY_PHRASES[normalized["energyCurve"]],
    ]
    if normalized["instruments"]:
        parts.append(
            "featuring "
            + ", ".join(
                INSTRUMENT_PHRASES[item] for item in normalized["instruments"]
            )
        )
    if normalized["loopable"]:
        parts.append("seamlessly loopable ending")
    if context_snapshot:
        parts.append(
            "scene context: "
            + "; ".join(
                value
                for value in (
                    context_snapshot["title"],
                    context_snapshot["mood"],
                    context_snapshot["summary"],
                )
                if value
            )
        )
    if normalized["textRefinement"]:
        parts.append(normalized["textRefinement"])
    positive_prompt = ". ".join(part for part in parts if part)[:MAX_PROMPT_CHARS]

    negative = [
        INSTRUMENT_PHRASES.get(item, "bright brass" if item == "bright_brass" else item)
        for item in normalized["exclude"]
    ]
    if normalized["content"]["mode"] == "instrumental":
        negative.extend(["vocals", "spoken words", "lyrics"])

    compiled = {
        "schemaVersion": "music-brief-v1",
        "contentMode": normalized["content"]["mode"],
        "durationSeconds": normalized["durationSeconds"],
        "variantCount": variant_count,
        "positivePrompt": positive_prompt,
        "negativePrompt": ", ".join(negative)[:1000],
        "controls": {
            key: deepcopy(normalized[key])
            for key in (
                "purpose",
                "genre",
                "moods",
                "tempo",
                "energyCurve",
                "instruments",
                "exclude",
                "loopable",
            )
        },
        "lyricsLanguage": normalized["content"].get("lyricsLanguage"),
        "vocalStyle": deepcopy(normalized["content"].get("vocalStyle", {})),
        "lyricsSections": deepcopy(normalized["content"].get("sections", [])),
        "sceneContext": context_snapshot,
        "referenceAssetId": (
            str(reference_asset_id) if reference_asset_id is not None else None
        ),
    }
    if "seed" in normalized:
        compiled["baseSeed"] = normalized["seed"]
    return compiled
