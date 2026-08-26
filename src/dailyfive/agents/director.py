"""Music Director — turns themes into checkable musical specification.

The distinction this agent exists to enforce: a prompt made of adjectives
produces a generation nobody can evaluate. A spec with a tempo, a key, a form
and a hook position produces one the QC Engineer can measure and the Archivist
can attribute a score to.

It reads the codex — including what the Archivist has learned — and writes back
proposed edits rather than mutating it directly.
"""

from __future__ import annotations

import json
import logging

from ..codex import Codex
from .base import ask_json

log = logging.getLogger(__name__)

SYSTEM = """You are the Music Director of an automated studio. You convert themes \
into production specifications precise enough that a generation can be checked \
against them afterwards.

Every spec you write must be CHECKABLE. Not "moody" — 84 BPM, F minor, hook at \
0:07, sub-heavy, no snare until bar 9. Not "energetic" — 128 BPM, four-on-floor, \
riser into a drop at 0:48.

You encode CHARACTERISTICS, never NAMES. Describing production technique, era, \
instrumentation and arrangement is your job. Naming a living recording artist as \
a target is not — it gets the generation refused by the model's own content \
filter, and it is not defensible even when it works. If you catch yourself \
reaching for "sounds like X", write down what X actually does instead.

Two slot types, briefed differently:
- FULL: 2.5-3.5 minute songs. Arrangement matters, dynamics matter, the second \
verse should not be the first verse again.
- SHORT: 30-60 second cuts built to loop. Hook inside the first two seconds. No \
intro. The loop seam must be inaudible, so the last bar has to lead back into \
the first.

Respect the codex's learned observations where they exist. They come from real \
measured outcomes, and they beat your priors. Where the codex is empty, say so \
rather than inventing a track record."""

SCHEMA = """{
  "specs": [
    {
      "theme": "the theme this spec serves, copied verbatim",
      "slot_type": "full|short",
      "bpm": 0,
      "key": "F minor",
      "song_form": "Intro(4) - Verse(16) - ...",
      "instrumentation": "concrete palette, <= 200 chars",
      "hook_note": "where the hook lands and what makes it stick, <= 160 chars",
      "vocal_gender": "m|f",
      "style_string": "comma-separated production descriptors for the generator, <= 900 chars, NO artist names",
      "mix_note": "<= 120 chars"
    }
  ],
  "codex_notes": ["observations worth carrying into the codex, <= 3 items"]
}"""


def run(themes: list[dict], codex: Codex, *, full_n: int, short_n: int,
        calibration: dict | None = None) -> list[dict]:
    """Produce full_n + short_n musical specs from the ranked themes."""
    want = full_n + short_n
    usable = themes[:max(want, len(themes))]

    user = (
        f"Write {full_n} FULL specs and {short_n} SHORT specs — {want} in total.\n\n"
        f"Today's ranked themes:\n{json.dumps(usable, ensure_ascii=False)}\n\n"
        f"Sonic calibration from the lagging feeds:\n"
        f"{json.dumps(calibration or {}, ensure_ascii=False)}\n\n"
        f"Codex v{codex.version}:\n{codex.brief_context()}\n\n"
        f"Available song forms — full: "
        f"{json.dumps(codex.body.get('song_forms', {}).get('full', []))}\n"
        f"Available song forms — short: "
        f"{json.dumps(codex.body.get('song_forms', {}).get('short', []))}\n\n"
        "Spread the specs across tempo bands and moods. Two specs that would "
        "produce the same song are a wasted slot."
    )

    result = ask_json(SYSTEM, user, schema_hint=SCHEMA, max_tokens=6000, temperature=1.0)
    specs = result.get("specs") or []

    out: list[dict] = []
    for spec in specs:
        st = spec.get("slot_type")
        if st not in ("full", "short"):
            continue
        style = str(spec.get("style_string") or "").strip()
        if not style:
            continue
        out.append({
            "theme": str(spec.get("theme") or "")[:300],
            "slot_type": st,
            "bpm": _int_or_none(spec.get("bpm")),
            "key": str(spec.get("key") or "")[:40] or None,
            "song_form": str(spec.get("song_form") or "")[:400] or None,
            "instrumentation": str(spec.get("instrumentation") or "")[:300] or None,
            "hook_note": str(spec.get("hook_note") or "")[:240],
            "vocal_gender": spec.get("vocal_gender") if spec.get("vocal_gender") in ("m", "f") else None,
            "style_string": style[:900],
            "mix_note": str(spec.get("mix_note") or "")[:200],
        })

    full = [s for s in out if s["slot_type"] == "full"][:full_n]
    short = [s for s in out if s["slot_type"] == "short"][:short_n]

    if len(full) < full_n or len(short) < short_n:
        log.warning("director returned %d full / %d short, wanted %d / %d",
                    len(full), len(short), full_n, short_n)

    notes = [str(n)[:200] for n in (result.get("codex_notes") or [])][:3]
    for spec in full + short:
        spec["_codex_notes"] = notes
    return full + short


def _int_or_none(v) -> int | None:
    try:
        n = int(v)
        return n if 40 <= n <= 220 else None
    except (TypeError, ValueError):
        return None
