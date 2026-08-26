"""Prompt Compiler — brief in, valid Suno payload out.

Almost entirely deterministic, and that is the point. This is the last place a
mistake is cheap: an over-long style string is silently truncated by the API, an
invalid parameter combination is rejected after the request has been counted,
and either way you find out from a bad song rather than an error.

So every limit is checked here, against the table for the specific model being
called, before anything leaves the building.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import settings
from ..errors import ConfigError
from ..providers.suno import DURATION_MODELS, MODEL_LIMITS

log = logging.getLogger(__name__)

# Defaults the Archivist can move as evidence accumulates.
DEFAULT_STYLE_WEIGHT = 0.65
DEFAULT_WEIRDNESS = 0.35
DEFAULT_AUDIO_WEIGHT = 0.65


class CompileWarning(dict):
    """A non-fatal finding, recorded on the payload for the Archivist."""


def compile_payload(brief: dict, *, model: str | None = None,
                    negative_tags: str | None = None,
                    style_weight: float | None = None,
                    weirdness: float | None = None,
                    audio_weight: float | None = None) -> dict[str, Any]:
    """Build one validated ``POST /api/v1/generate`` body.

    Raises ConfigError on anything that would certainly fail. Returns the
    payload with a ``_warnings`` key for anything survivable.
    """
    cfg = settings()
    model = model or cfg.suno_model
    limits = MODEL_LIMITS.get(model)
    if limits is None:
        raise ConfigError(
            f"unknown Suno model {model!r} — known: {', '.join(sorted(MODEL_LIMITS))}")

    warnings: list[str] = []
    slot_type = brief.get("slot_type", "full")

    style = _fit(brief.get("style_string") or "", limits["style"], "style", warnings)
    title = _fit(brief.get("title") or "Untitled", limits["title"], "title", warnings)
    lyrics = _fit(brief.get("lyrics") or "", limits["prompt"], "prompt", warnings)

    if not style:
        raise ConfigError(f"brief {title!r} has no style string — nothing to generate from")
    if not lyrics:
        raise ConfigError(f"brief {title!r} has no lyrics — custom mode requires them")

    payload: dict[str, Any] = {
        # Custom mode with vocals requires style, title and prompt together.
        "customMode": True,
        "instrumental": False,
        "model": model,
        "style": style,
        "title": title,
        "prompt": lyrics,
        "callBackUrl": cfg.callback_url("generate"),
    }

    neg = negative_tags if negative_tags is not None else brief.get("negative_tags")
    if neg:
        payload["negativeTags"] = _fit(str(neg), 900, "negativeTags", warnings)

    gender = brief.get("vocal_gender")
    if gender in ("m", "f"):
        payload["vocalGender"] = gender
    elif gender:
        warnings.append(f"dropped vocalGender={gender!r}; only 'm' or 'f' are valid")

    payload["styleWeight"] = _weight(style_weight, DEFAULT_STYLE_WEIGHT)
    payload["weirdnessConstraint"] = _weight(weirdness, DEFAULT_WEIRDNESS)
    payload["audioWeight"] = _weight(audio_weight, DEFAULT_AUDIO_WEIGHT)

    persona_id = brief.get("persona_id")
    if persona_id:
        payload["personaId"] = persona_id
        payload["personaModel"] = brief.get("persona_model") or "style_persona"
    elif brief.get("persona_name"):
        warnings.append(
            f"persona {brief['persona_name']!r} has no persona_id — this will render "
            "with a generic voice. Run `dailyfive personas bootstrap`.")

    # Duration is V5_5-only. Sending it to another model is a parameter error,
    # not a silently ignored field.
    if slot_type == "short":
        if model in DURATION_MODELS:
            payload["duration"] = _duration(cfg.short_duration_s, warnings)
        else:
            warnings.append(
                f"short cut requested but model {model} does not accept duration — "
                "the model will choose its own length")

    if warnings:
        for w in warnings:
            log.warning("compile %r: %s", title, w)
    payload["_warnings"] = warnings
    return payload


def strip_internal(payload: dict) -> dict:
    """The wire version: everything except our own bookkeeping keys."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _fit(value: str, limit: int, field: str, warnings: list[str]) -> str:
    """Truncate at a sane boundary and say so, rather than letting the API do it.

    Silent truncation server-side is the failure this whole module exists to
    prevent, so a truncation here is always recorded.
    """
    value = (value or "").strip()
    if len(value) <= limit:
        return value

    warnings.append(f"{field} was {len(value)} chars, truncated to {limit}")
    cut = value[:limit]
    # Prefer a clause or line boundary in the last 15% of the budget.
    window = max(1, limit * 85 // 100)
    for sep in ("\n\n", "\n", ". ", ", "):
        idx = cut.rfind(sep)
        if idx >= window:
            return cut[:idx].strip()
    return cut.strip()


def _weight(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return round(max(0.0, min(1.0, v)), 2)


def _duration(seconds: int, warnings: list[str]) -> int:
    if not 10 <= seconds <= 360:
        warnings.append(f"duration {seconds}s outside 10-360, clamped")
    return max(10, min(360, int(seconds)))


def validate(payload: dict) -> list[str]:
    """Independent re-check of a payload. Used by tests and the Conductor.

    Deliberately a second implementation of the same rules rather than a call
    back into the builder — a validator that shares code with the thing it
    validates cannot catch a bug in the shared part.
    """
    problems: list[str] = []
    model = payload.get("model")
    limits = MODEL_LIMITS.get(model or "")
    if limits is None:
        return [f"unknown model {model!r}"]

    if payload.get("customMode") is not True:
        problems.append("customMode must be true for this pipeline")
    if payload.get("instrumental") is not False:
        problems.append("instrumental must be false — these songs have vocals")

    for field, key in (("style", "style"), ("title", "title"), ("prompt", "prompt")):
        val = payload.get(field)
        if not val:
            problems.append(f"{field} is required in custom mode with vocals")
        elif len(val) > limits[key]:
            problems.append(f"{field} is {len(val)} chars, over the {limits[key]} limit for {model}")

    if not payload.get("callBackUrl"):
        problems.append("callBackUrl is required — Suno is asynchronous")

    for w in ("styleWeight", "weirdnessConstraint", "audioWeight"):
        v = payload.get(w)
        if v is not None and not (0.0 <= float(v) <= 1.0):
            problems.append(f"{w}={v} outside 0.00-1.00")

    g = payload.get("vocalGender")
    if g is not None and g not in ("m", "f"):
        problems.append(f"vocalGender={g!r} must be 'm' or 'f'")

    if "duration" in payload:
        if model not in DURATION_MODELS:
            problems.append(f"duration is only valid on {sorted(DURATION_MODELS)}, not {model}")
        elif not 10 <= payload["duration"] <= 360:
            problems.append(f"duration={payload['duration']} outside 10-360")

    if payload.get("personaId") and payload.get("personaModel") not in \
            ("style_persona", "voice_persona"):
        problems.append("personaModel must be 'style_persona' or 'voice_persona'")

    return problems
