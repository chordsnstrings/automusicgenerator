"""Scout — fuses seven free feeds into one ranked signal sheet.

The whole job is weighting by how *leading* a source is. A chart position tells
you what already happened; a search term spiking tells you what is about to.
The free stack is strong on theme and sentiment and weak on sound velocity, and
the prompt says so explicitly rather than inviting the model to invent
confidence it does not have.
"""

from __future__ import annotations

import json
import logging

from ..signals import FeedResult, collect_all
from .base import ask_json, clamp

log = logging.getLogger(__name__)

SYSTEM = """You are the Scout for an automated music studio that releases five \
songs a day.

Your job is to read today's raw feeds and produce a ranked sheet of THEMES worth \
writing songs about — not a list of songs that are already popular. Nobody needs \
another copy of what already charted.

Weight your sources by how far ahead of the market they sit:
- LEADING (Google Trends searches, Reddit discourse): people are talking about \
this before anyone has written the song. Weight these heaviest for theme.
- MODERATE (YouTube chart, Genius): confirms a theme has commercial traction.
- LAGGING (Apple, Deezer, Last.fm): tells you the current sonic centre of \
gravity — tempo, genre mix, duration norms. Use for calibration, not for ideas.

Known limits of this stack, which you must respect rather than paper over:
- There is NO TikTok velocity signal here. Do not claim to know what is rising \
on TikTok. If a theme's evidence is thin, say so in the confidence score.
- Feeds marked unavailable contributed nothing. Do not invent evidence from them.

For each theme give a specific emotional situation, not a genre or a mood word. \
"Missing someone you chose to leave" is a theme. "Sad" is not. "Summer vibes" is \
not. Concrete, human, and specific enough that two different lyricists would \
write recognisably different songs from it.

Never name a living recording artist as a stylistic target."""

SCHEMA = """{
  "themes": [
    {
      "theme": "specific emotional situation, <= 120 chars",
      "sentiment": "the dominant feeling, 1-3 words",
      "evidence": "which feeds support this and how, <= 240 chars",
      "sources": ["gtrends", "reddit"],
      "lead": "leading|moderate|lagging",
      "confidence": 0.0
    }
  ],
  "sonic_calibration": {
    "tempo_centre": 0,
    "tempo_range": [0, 0],
    "duration_norm_s": 0,
    "genre_mix": ["..."],
    "notes": "what the lagging feeds say about the current centre of gravity"
  }
}"""


def run(region: str = "US", *, want: int = 10,
        feeds: list[FeedResult] | None = None) -> dict:
    """Collect, fuse, rank. Returns the signal sheet."""
    feeds = feeds if feeds is not None else collect_all(region)
    live = [f for f in feeds if f.ok]
    dead = [f for f in feeds if not f.ok]

    log.info("scout: %d/%d feeds live", len(live), len(feeds))
    for f in feeds:
        log.info("  %s", f.summary())

    if not live:
        raise RuntimeError(
            "every trend feed failed — refusing to invent themes from nothing. "
            "Check network access and the four optional API keys.")

    payload = {
        "date_region": region,
        "available": [{"source": f.source, "lead": f.lead, "items": f.items[:30]}
                      for f in live],
        "unavailable": [{"source": f.source, "why": f.error} for f in dead],
    }
    user = (
        f"Produce {want} ranked themes from today's feeds.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)[:60000]}"
    )

    result = ask_json(SYSTEM, user, schema_hint=SCHEMA, max_tokens=6000, temperature=1.0)
    themes = result.get("themes") or []

    cleaned = []
    for i, t in enumerate(themes[:want]):
        if not (t.get("theme") or "").strip():
            continue
        cleaned.append({
            "rank": i + 1,
            "theme": str(t["theme"])[:200],
            "sentiment": str(t.get("sentiment", ""))[:80],
            "evidence": str(t.get("evidence", ""))[:500],
            "sources": [str(x) for x in (t.get("sources") or [])][:8],
            "lead": t.get("lead") if t.get("lead") in
                    ("leading", "moderate", "lagging") else "moderate",
            "confidence": clamp(t.get("confidence"), 0.0, 1.0, 0.5),
        })

    if not cleaned:
        raise RuntimeError("scout produced no usable themes")

    return {
        "themes": cleaned,
        "sonic_calibration": result.get("sonic_calibration") or {},
        "feeds_live": [f.source for f in live],
        "feeds_dead": {f.source: f.error for f in dead},
    }
