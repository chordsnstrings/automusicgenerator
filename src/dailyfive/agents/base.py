"""Shared Claude plumbing: one call, strict JSON out, one repair attempt.

Structured output is enforced by asking for a schema and re-prompting with the
parse error on failure. A second failure raises rather than guessing, because a
silently-defaulted brief is worse than a run that stops and says why.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..config import settings
from ..errors import ProviderError

log = logging.getLogger(__name__)

PROVIDER = "claude"
_client = None


def client():
    global _client
    if _client is None:
        import anthropic
        cfg = settings()
        if not cfg.anthropic_api_key:
            raise ProviderError(PROVIDER, "ANTHROPIC_API_KEY is not set", retryable=False)
        _client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    return _client


def ask(system: str, user: str, *, max_tokens: int = 4000,
        temperature: float = 1.0, model: str | None = None) -> str:
    """One completion, text out."""
    cfg = settings()
    try:
        resp = client().messages.create(
            model=model or cfg.anthropic_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:
        retryable = any(t in str(exc).lower() for t in
                        ("overload", "rate", "timeout", "503", "529", "500"))
        raise ProviderError(PROVIDER, f"messages.create failed: {exc}",
                            retryable=retryable) from exc

    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


def ask_json(system: str, user: str, *, schema_hint: str,
             max_tokens: int = 4000, temperature: float = 1.0,
             model: str | None = None) -> Any:
    """Ask for JSON and get JSON, or raise.

    The repair turn feeds the actual parse error back rather than just saying
    "invalid" — a model told which bracket it dropped fixes it; one told to
    "try again" usually produces the same output.
    """
    full_system = (
        f"{system}\n\n"
        "Respond with a single JSON value and nothing else. No prose before or "
        "after, no markdown fence, no trailing commas.\n"
        f"Shape:\n{schema_hint}"
    )
    raw = ask(full_system, user, max_tokens=max_tokens,
              temperature=temperature, model=model)
    try:
        return _parse(raw)
    except ValueError as exc:
        log.warning("JSON repair turn: %s", exc)
        repair = (
            f"{user}\n\n---\nYour previous reply could not be parsed as JSON.\n"
            f"Parse error: {exc}\nPrevious reply began: {raw[:400]!r}\n"
            "Return only the corrected JSON."
        )
        raw2 = ask(full_system, repair, max_tokens=max_tokens,
                   temperature=0.2, model=model)
        try:
            return _parse(raw2)
        except ValueError as exc2:
            raise ProviderError(PROVIDER, f"model would not produce valid JSON: {exc2}",
                                retryable=False) from exc2


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _parse(raw: str) -> Any:
    """Tolerate a fence or surrounding prose; refuse anything worse."""
    text = raw.strip()
    if not text:
        raise ValueError("empty response")

    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as first:
        # Last resort: the outermost balanced object or array in the text.
        span = _outermost(text)
        if span is None:
            raise ValueError(str(first)) from first
        try:
            return json.loads(span)
        except json.JSONDecodeError as second:
            raise ValueError(str(second)) from second


def _outermost(text: str) -> str | None:
    starts = [i for i, ch in enumerate(text) if ch in "{["]
    if not starts:
        return None
    start = starts[0]
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default
