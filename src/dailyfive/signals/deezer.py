"""Deezer charts. No auth. The second opinion on what genre is charting.

This feed used to be here for BPM, on the strength of the chart row's per-track
``bpm`` field. That field is empty precisely where it would matter: Deezer's
audio analysis lags ingestion, so ``/track/{id}`` returned ``bpm: 0`` for all
ten of today's chart tracks, and every one before that. A tempo signal that is
zero on new releases is not a tempo signal.

Genre is real and is on neither the chart row nor the track — only on
``/album/{id}``, which the chart row already gives us the id for. So the ten
enrichment calls are spent there instead: same request budget, a populated
field instead of an empty one. It is a genuinely independent read, and the
disagreement is the useful part — the same day Apple's US chart says country
leads pop 23 to 6, Deezer says pop leads country. Two feeds that agree tell you
one thing; two that disagree tell you the question is open.
"""

from __future__ import annotations

from ..http import request
from . import FeedResult

CHART_URL = "https://api.deezer.com/chart/0/tracks"
ALBUM_URL = "https://api.deezer.com/album/{id}"


def fetch(limit: int = 25, *, with_genre: int = 10) -> FeedResult:
    try:
        resp = request("GET", CHART_URL, provider="deezer", params={"limit": limit},
                       timeout=30.0, attempts=2)
        if resp.status_code >= 400:
            return FeedResult("deezer", "lagging", error=f"HTTP {resp.status_code}")
        data = resp.json()
    except Exception as exc:
        return FeedResult("deezer", "lagging", error=str(exc)[:200])

    if "error" in data:
        return FeedResult("deezer", "lagging", error=str(data["error"])[:200])

    rows = data.get("data") or []
    items = []
    for i, t in enumerate(rows):
        items.append({
            "rank": t.get("position", i + 1),
            "title": t.get("title_short") or t.get("title"),
            "artist": (t.get("artist") or {}).get("name"),
            "duration_s": t.get("duration"),
            "genres": [],
            "genre_ids": [],
        })

    # Genre needs a per-album call, so only the top few are enriched — enough
    # to see where the chart sits without spending 25 round trips on it. An
    # album carries several genres and they are kept all: "Country, Pop" on a
    # crossover single is the record being two things, and picking one of them
    # here would be inventing a precision the tag does not have.
    for i, t in enumerate(rows[:with_genre]):
        album_id = (t.get("album") or {}).get("id")
        if not album_id:
            continue
        try:
            r = request("GET", ALBUM_URL.format(id=album_id), provider="deezer",
                        timeout=15.0, attempts=1)
            if r.status_code == 200:
                genres = ((r.json().get("genres") or {}).get("data") or [])
                items[i]["genres"] = [g["name"] for g in genres if g.get("name")]
                items[i]["genre_ids"] = [str(g["id"]) for g in genres
                                         if g.get("id") is not None]
        except Exception:  # enrichment is optional; the chart row still stands
            continue

    if not items:
        return FeedResult("deezer", "lagging", error="empty chart")
    return FeedResult("deezer", "lagging", items=items)
