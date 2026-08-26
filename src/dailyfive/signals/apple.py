"""Apple Music — most-played. No auth, no key, no rate limit worth worrying about.

Authoritative but lagging: by the time a track is here, the moment that made it
has already happened. Useful as ground truth against which the leading feeds
are calibrated, not as a thing to chase.
"""

from __future__ import annotations

from ..http import request
from . import FeedResult

URL = "https://rss.applemarketingtools.com/api/v2/{region}/music/most-played/{limit}/songs.json"


def fetch(region: str = "US", limit: int = 50) -> FeedResult:
    url = URL.format(region=region.lower(), limit=limit)
    try:
        resp = request("GET", url, provider="apple", timeout=30.0, attempts=2)
        if resp.status_code >= 400:
            return FeedResult("apple", "lagging", error=f"HTTP {resp.status_code}")
        feed = resp.json().get("feed") or {}
    except Exception as exc:
        return FeedResult("apple", "lagging", error=str(exc)[:200])

    items = [
        {
            "rank": i + 1,
            "title": e.get("name"),
            "artist": e.get("artistName"),
            "genres": [g.get("name") for g in (e.get("genres") or []) if g.get("name")],
            "released": e.get("releaseDate"),
        }
        for i, e in enumerate(feed.get("results") or [])
    ]
    if not items:
        return FeedResult("apple", "lagging", error="empty feed")
    return FeedResult("apple", "lagging", items=items)
