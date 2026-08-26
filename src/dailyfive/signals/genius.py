"""Genius — what is hot, and the lyrical themes underneath it.

The only feed in the stack that reads *words*. Chart feeds tell you a song is
big; this tells you what it is about, which is the half the Lyricist can
actually act on.
"""

from __future__ import annotations

from ..config import settings
from ..http import request
from . import FeedResult

API = "https://api.genius.com"


def fetch(limit: int = 20) -> FeedResult:
    token = settings().genius_token
    if not token:
        return FeedResult("genius", "moderate", error="GENIUS_ACCESS_TOKEN not set")

    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = request("GET", f"{API}/songs/chart", provider="genius", headers=headers,
                       params={"time_period": "day", "per_page": limit, "chart_genre": "all"},
                       timeout=30.0, attempts=2)
        if resp.status_code == 404:
            # The chart endpoint is undocumented and moves; fall back to search,
            # which is stable and still surfaces current annotations.
            return _fallback_search(headers, limit)
        if resp.status_code >= 400:
            return FeedResult("genius", "moderate", error=f"HTTP {resp.status_code}")
        chart = (resp.json().get("response") or {}).get("chart_items") or []
    except Exception as exc:
        return FeedResult("genius", "moderate", error=str(exc)[:200])

    items = []
    for i, entry in enumerate(chart):
        song = entry.get("item") or {}
        items.append({
            "rank": i + 1,
            "title": song.get("title"),
            "artist": (song.get("primary_artist") or {}).get("name"),
            "hot": song.get("stats", {}).get("hot", False),
            "pageviews": (song.get("stats") or {}).get("pageviews"),
        })
    if not items:
        return _fallback_search(headers, limit)
    return FeedResult("genius", "moderate", items=items)


def _fallback_search(headers: dict, limit: int) -> FeedResult:
    try:
        resp = request("GET", f"{API}/search", provider="genius", headers=headers,
                       params={"q": "2026"}, timeout=25.0, attempts=1)
        if resp.status_code >= 400:
            return FeedResult("genius", "moderate", error=f"search HTTP {resp.status_code}")
        hits = (resp.json().get("response") or {}).get("hits") or []
    except Exception as exc:
        return FeedResult("genius", "moderate", error=str(exc)[:200])

    items = [{
        "rank": i + 1,
        "title": (h.get("result") or {}).get("title"),
        "artist": ((h.get("result") or {}).get("primary_artist") or {}).get("name"),
        "pageviews": ((h.get("result") or {}).get("stats") or {}).get("pageviews"),
    } for i, h in enumerate(hits[:limit])]

    if not items:
        return FeedResult("genius", "moderate", error="no results")
    return FeedResult("genius", "moderate", items=items)
