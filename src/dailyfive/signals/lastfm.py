"""Last.fm — top tracks and, more usefully, tag drift.

Chart position here lags. What does not lag is which tags are attached to what
is rising: a genre label moving through the tag cloud is an early read on where
a sound is going, and it is the one thing this feed gives that the others do not.
"""

from __future__ import annotations

from ..config import settings
from ..http import request
from . import FeedResult

URL = "https://ws.audioscrobbler.com/2.0/"


def fetch(limit: int = 30) -> FeedResult:
    key = settings().lastfm_api_key
    if not key:
        return FeedResult("lastfm", "lagging", error="LASTFM_API_KEY not set")

    items: list[dict] = []
    try:
        resp = request("GET", URL, provider="lastfm", timeout=30.0, attempts=2, params={
            "method": "chart.gettoptracks", "api_key": key,
            "format": "json", "limit": limit,
        })
        if resp.status_code >= 400:
            return FeedResult("lastfm", "lagging", error=f"HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data:
            return FeedResult("lastfm", "lagging", error=str(data.get("message"))[:160])
        for i, t in enumerate((data.get("tracks") or {}).get("track") or []):
            items.append({
                "rank": i + 1,
                "title": t.get("name"),
                "artist": (t.get("artist") or {}).get("name"),
                "listeners": _int(t.get("listeners")),
            })
    except Exception as exc:
        return FeedResult("lastfm", "lagging", error=str(exc)[:200])

    # Tag drift — the reason this feed earns its slot.
    tags: list[dict] = []
    try:
        r = request("GET", URL, provider="lastfm", timeout=20.0, attempts=1, params={
            "method": "chart.gettoptags", "api_key": key, "format": "json", "limit": 25,
        })
        if r.status_code == 200:
            for t in (r.json().get("tags") or {}).get("tag") or []:
                tags.append({"tag": t.get("name"), "reach": _int(t.get("reach"))})
    except Exception:
        pass

    if not items:
        return FeedResult("lastfm", "lagging", error="empty chart")
    if tags:
        items.append({"_tags": tags})
    return FeedResult("lastfm", "lagging", items=items)


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
