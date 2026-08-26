"""YouTube Data API v3 — the most-popular music chart per region.

Free tier is 10,000 quota units a day and this costs 1 unit per call, so the
budget is irrelevant. Category 10 is Music. Tags and descriptions come back
with the videos, which is what makes this more useful than a bare chart: the
Scout reads the language creators are using, not just the ranking.
"""

from __future__ import annotations

from ..config import settings
from ..http import request
from . import FeedResult

URL = "https://www.googleapis.com/youtube/v3/videos"
MUSIC_CATEGORY = "10"


def fetch(region: str = "US", limit: int = 30) -> FeedResult:
    key = settings().youtube_api_key
    if not key:
        return FeedResult("youtube", "moderate", error="YOUTUBE_API_KEY not set")

    params = {
        "part": "snippet,statistics",
        "chart": "mostPopular",
        "videoCategoryId": MUSIC_CATEGORY,
        "regionCode": region,
        "maxResults": min(limit, 50),
        "key": key,
    }
    try:
        resp = request("GET", URL, provider="youtube", params=params, timeout=30.0, attempts=2)
        if resp.status_code >= 400:
            detail = resp.json().get("error", {}).get("message", resp.text[:120])
            return FeedResult("youtube", "moderate", error=f"HTTP {resp.status_code}: {detail}")
        data = resp.json()
    except Exception as exc:
        return FeedResult("youtube", "moderate", error=str(exc)[:200])

    items = []
    for i, v in enumerate(data.get("items") or []):
        sn = v.get("snippet") or {}
        st = v.get("statistics") or {}
        items.append({
            "rank": i + 1,
            "title": sn.get("title"),
            "channel": sn.get("channelTitle"),
            "published": sn.get("publishedAt"),
            "tags": (sn.get("tags") or [])[:12],
            "views": _int(st.get("viewCount")),
            "likes": _int(st.get("likeCount")),
        })
    if not items:
        return FeedResult("youtube", "moderate", error="empty chart")
    return FeedResult("youtube", "moderate", items=items)


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
