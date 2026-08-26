"""Shared agent plumbing: one call, strict JSON out, one repair attempt.

Every call names a *role*, never a vendor — :mod:`dailyfive.llm` decides which
brain answers. That is what lets the whole roster move to MiniMax, to a local
model, or to a mix, without editing an agent.

Structured output is enforced three ways, in descending order of strength:
native JSON mode where the provider has one, the schema in the prompt, and a
repair turn that feeds the actual parse error back. A second failure raises
rather than guessing, because a silently-defaulted brief is worse than a run
that stops and says why.

Every call is recorded on a per-run ledger, so the console can show which brain
wrote what, how long it took, and what it cost.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..errors import ProviderError
from ..llm import Brain, complete
from ..ledger import record_call

log = logging.getLogger(__name__)


def ask(role: str, system: str, user: str, *, max_tokens: int = 4000,
        temperature: float = 1.0, json_mode: bool = False,
        label: str = "") -> str:
    """One completion for a role. Text out; the brain choice is logged."""
    started = time.monotonic()
    try:
        text, brain = complete(role, system, user, max_tokens=max_tokens,
                               temperature=temperature, json_mode=json_mode)
    except ProviderError as exc:
        record_call(role, None, label or role, ok=False,
                    ms=int((time.monotonic() - started) * 1000),
                    chars_in=len(system) + len(user), chars_out=0, error=str(exc))
        raise
    record_call(role, brain, label or role, ok=True,
                ms=int((time.monotonic() - started) * 1000),
                chars_in=len(system) + len(user), chars_out=len(text))
    return text


def ask_json(role: str, system: str, user: str, *, schema_hint: str,
             max_tokens: int = 4000, temperature: float = 1.0,
             label: str = "") -> Any:
    """Ask for JSON and get JSON, or raise.

    The repair turn feeds the actual parse error back rather than just saying
    "invalid" — a model told which bracket it dropped fixes it; one told to
    "try again" usually produces the same output. That matters more on smaller
    brains, which is exactly the case this layer exists to support.
    """
    full_system = (
        f"{system}\n\n"
        "Respond with a single JSON value and nothing else. No prose before or "
        "after, no markdown fence, no trailing commas.\n"
        f"Shape:\n{schema_hint}"
    )
    raw = ask(role, full_system, user, max_tokens=max_tokens,
              temperature=temperature, json_mode=True, label=label or role)
    try:
        return _parse(raw)
    except ValueError as exc:
        log.warning("%s: JSON repair turn — %s", role, exc)
        repair = (
            f"{user}\n\n---\nYour previous reply could not be parsed as JSON.\n"
            f"Parse error: {exc}\nPrevious reply began: {raw[:400]!r}\n"
            "Return only the corrected JSON."
        )
        raw2 = ask(role, full_system, repair, max_tokens=max_tokens,
                   temperature=0.2, json_mode=True, label=f"{label or role}:repair")
        try:
            return _parse(raw2)
        except ValueError as exc2:
            raise ProviderError("llm", f"{role}: brain would not produce valid "
                                       f"JSON after a repair turn: {exc2}",
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
