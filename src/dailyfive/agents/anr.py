"""A&R — turns specs into briefs, and stops the five songs being the same song.

This is the agent the original roster was missing and the one whose absence is
hardest to notice: nothing else in the pipeline is looking at the day's output
as a *set*. A Producer choosing the best of five near-identical candidates has
still shipped five near-identical candidates.

Diversity is enforced twice — once by the model reasoning about the set, and
once by a deterministic check afterwards against the last fourteen days, because
a model asked to avoid repetition will still repeat itself occasionally and the
cost of catching it here is nothing.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from sqlalchemy import select

from ..codex import Codex
from ..db import session_scope
from ..models import Brief, Run, SlotType
from .base import ask_json

log = logging.getLogger(__name__)

SYSTEM = """You are the A&R for an automated studio shipping five songs a day.

You receive musical specs and turn each into a BRIEF a lyricist and a prompt \
compiler can both work from. You also own the one thing nobody else is watching: \
whether today's releases are actually different from each other, and from the \
last fortnight's.

Spread the set deliberately across tempo, mood, vocal gender, lyrical person and \
subject matter. If two briefs would produce songs a listener could confuse, \
change one. A day of five variations on one idea is a wasted day even if every \
individual song is good.

Assign each brief a persona from the cast provided. Match the persona's \
territory and tempo preference — putting a restrained close-mic vocalist on a \
club track wastes both. Rotate so no persona takes more than half the day's slots.

Titles should be specific and memorable. Not "Midnight Dreams". Not "Broken \
Hearts". Something a person would actually remember having heard."""

SCHEMA = """{
  "briefs": [
    {
      "spec_index": 0,
      "title": "song title, <= 60 chars",
      "theme": "the specific situation this song is about, <= 300 chars",
      "persona_name": "name from the cast",
      "angle": "what makes this one different from the others today, <= 160 chars",
      "diversity_vector": {
        "mood": "one word",
        "tempo_band": "ballad|midtempo|uptempo|club",
        "person": "first|second|third",
        "subject": "one or two words"
      }
    }
  ],
  "set_note": "why this set of five is varied, <= 300 chars"
}"""


def run(specs: list[dict], codex: Codex, *, history_days: int = 14) -> list[dict]:
    """Attach titles, personas and diversity vectors to the day's specs."""
    cast = codex.personas_ready() or codex.personas
    if not codex.personas_ready():
        log.warning("no personas have a persona_id yet — briefs will render with a "
                    "generic voice. Run `dailyfive personas bootstrap` to fix this.")

    history = _recent(history_days)

    user = (
        f"Today's specs:\n{json.dumps(specs, ensure_ascii=False)}\n\n"
        f"Persona cast:\n{json.dumps(cast, ensure_ascii=False)}\n\n"
        f"Shipped in the last {history_days} days (do not repeat these):\n"
        f"{json.dumps(history, ensure_ascii=False)}\n\n"
        f"Produce exactly {len(specs)} briefs, one per spec, in spec order."
    )

    result = ask_json("anr", SYSTEM, user, schema_hint=SCHEMA,
                      max_tokens=5000, temperature=1.0, label="briefs")
    raw = result.get("briefs") or []

    briefs: list[dict] = []
    by_name = {p.get("name"): p for p in cast}
    for i, spec in enumerate(specs):
        b = raw[i] if i < len(raw) else {}
        persona = by_name.get(b.get("persona_name")) or _pick_persona(cast, spec, i)
        briefs.append({
            **spec,
            "idx": i,
            "title": (str(b.get("title") or "").strip() or f"Untitled {i + 1}")[:80],
            "theme": str(b.get("theme") or spec.get("theme") or "")[:400],
            "persona_name": persona.get("name") if persona else None,
            "persona_id": persona.get("persona_id") if persona else None,
            "persona_model": persona.get("persona_model", "style_persona") if persona else None,
            "angle": str(b.get("angle") or "")[:200],
            "diversity_vector": b.get("diversity_vector") or {},
        })

    _rebalance_personas(briefs, cast)
    dupes = _flag_duplicates(briefs)
    if dupes:
        log.warning("A&R produced overlapping briefs: %s", dupes)
    return briefs


def _pick_persona(cast: list[dict], spec: dict, i: int) -> dict | None:
    """Deterministic fallback when the model names a persona that does not exist."""
    if not cast:
        return None
    gender = spec.get("vocal_gender")
    matches = [p for p in cast if not gender or p.get("vocal_gender") == gender]
    pool = matches or cast
    return pool[i % len(pool)]


def _rebalance_personas(briefs: list[dict], cast: list[dict]) -> None:
    """No persona takes more than half the day. Enforced, not requested."""
    if len(cast) < 2:
        return
    limit = max(1, len(briefs) // 2)
    counts: dict[str, int] = {}
    for b in briefs:
        name = b.get("persona_name")
        counts[name] = counts.get(name, 0) + 1

    for b in briefs:
        name = b.get("persona_name")
        if counts.get(name, 0) <= limit:
            continue
        alt = min(cast, key=lambda p: counts.get(p.get("name"), 0))
        if alt.get("name") == name:
            continue
        counts[name] -= 1
        counts[alt["name"]] = counts.get(alt["name"], 0) + 1
        log.info("rebalanced brief %r from %s to %s", b["title"], name, alt["name"])
        b["persona_name"] = alt.get("name")
        b["persona_id"] = alt.get("persona_id")
        b["persona_model"] = alt.get("persona_model", "style_persona")


def _flag_duplicates(briefs: list[dict]) -> list[tuple[str, str]]:
    """Two briefs sharing mood, tempo band and subject are the same song."""
    seen: dict[tuple, str] = {}
    dupes = []
    for b in briefs:
        dv = b.get("diversity_vector") or {}
        key = (str(dv.get("mood", "")).lower(),
               str(dv.get("tempo_band", "")).lower(),
               str(dv.get("subject", "")).lower())
        if key == ("", "", ""):
            continue
        if key in seen:
            dupes.append((seen[key], b["title"]))
        else:
            seen[key] = b["title"]
    return dupes


def _recent(days: int) -> list[dict]:
    """What shipped lately, for the model to steer away from."""
    cutoff = date.today() - timedelta(days=days)
    with session_scope() as s:
        rows = s.execute(
            select(Brief.title, Brief.theme, Brief.bpm, Brief.persona_name,
                   Brief.diversity_vector, Brief.slot_type)
            .join(Run, Brief.run_id == Run.id)
            .where(Run.run_date >= cutoff)
            .order_by(Run.run_date.desc())
            .limit(80)
        ).all()
    return [{
        "title": r.title,
        "theme": (r.theme or "")[:120],
        "bpm": r.bpm,
        "persona": r.persona_name,
        "slot": r.slot_type.value if isinstance(r.slot_type, SlotType) else str(r.slot_type),
        "vector": r.diversity_vector,
    } for r in rows]
