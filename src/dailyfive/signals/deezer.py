"""Deezer charts. No auth. Track objects carry BPM, which nothing else free does.

That BPM field is the reason this feed is in the stack at all — it lets the
Music Director check its tempo bands against what is actually charting rather
than against a recollection of what usually charts.
"""

from __future__ import annotations

from ..http import request
from . import FeedResult

CHART_URL = "https://api.deezer.com/chart/0/tracks"
TRACK_URL = "https://api.deezer.com/track/{id}"


def fetch(limit: int = 25, *, with_bpm: int = 10) -> FeedResult:
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

    items = []
    for i, t in enumerate(data.get("data") or []):
        items.append({
            "rank": t.get("position", i + 1),
            "title": t.get("title_short") or t.get("title"),
            "artist": (t.get("artist") or {}).get("name"),
            "duration_s": t.get("duration"),
            "bpm": None,
        })

    # BPM needs a per-track call, so only the top few are enriched — enough to
    # see where the tempo band sits without spending 25 round trips on it.
    for i, t in enumerate((data.get("data") or [])[:with_bpm]):
        tid = t.get("id")
        if not tid:
            continue
        try:
            r = request("GET", TRACK_URL.format(id=tid), provider="deezer",
                        timeout=15.0, attempts=1)
            if r.status_code == 200:
                detail = r.json()
                items[i]["bpm"] = detail.get("bpm") or None
                items[i]["gain"] = detail.get("gain")
        except Exception:  # enrichment is optional; the chart row still stands
            continue

    if not items:
        return FeedResult("deezer", "lagging", error="empty chart")
    return FeedResult("deezer", "lagging", items=items)
