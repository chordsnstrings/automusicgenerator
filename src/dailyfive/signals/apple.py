"""Apple Music — most-played. No auth, no key, no rate limit worth worrying about.

Authoritative but lagging: by the time a track is here, the moment that made it
has already happened. Useful as ground truth against which the leading feeds
are calibrated, not as a thing to chase.

Two things about this feed have to be handled here or every reader downstream
gets them wrong. Genre objects carry a stable ``genreId`` beside a localised
``name``, so the id is what identifies a genre and the name is decoration. And
the results blend new releases with deep catalogue: on the US feed today 31 of
50 entries are over a year old, eight of them one artist's back catalogue
riding a news cycle. Counted together, country beats pop 23 to 6; counted apart,
current releases are country 6, pop 6 — a tie. Anything that consumes this feed
as one number is reading the wrong one, so the age is attached per entry here
and the two are never added together.
"""

from __future__ import annotations

from datetime import date

from ..genres import APPLE_UMBRELLA_IDS
from ..http import request
from . import FeedResult

URL = "https://rss.applemarketingtools.com/api/v2/{region}/music/most-played/{limit}/songs.json"

# A release stops being news at a year. The cut has to fall somewhere and no
# boundary is principled; a year is long enough that a slow-burning single is
# still current and short enough that a 1973 recording is not.
CURRENT_DAYS = 365


def _window(released: object, today: date) -> str | None:
    """"current" or "catalogue" for an entry, or None when the date is unusable."""
    try:
        age = (today - date.fromisoformat(str(released))).days
    except (TypeError, ValueError):
        return None
    return "current" if age <= CURRENT_DAYS else "catalogue"


def fetch(region: str = "US", limit: int = 50) -> FeedResult:
    url = URL.format(region=region.lower(), limit=limit)
    try:
        resp = request("GET", url, provider="apple", timeout=30.0, attempts=2)
        if resp.status_code >= 400:
            return FeedResult("apple", "lagging", error=f"HTTP {resp.status_code}")
        feed = resp.json().get("feed") or {}
    except Exception as exc:
        return FeedResult("apple", "lagging", error=str(exc)[:200])

    today = date.today()
    items = []
    for i, e in enumerate(feed.get("results") or []):
        # genreId "34" is "Music", and it is on all 50 of 50 entries in every
        # region checked. It is the root of Apple's genre tree, not a genre,
        # and left in it is the largest count on every chart.
        genres = [g for g in (e.get("genres") or [])
                  if str(g.get("genreId") or "") not in APPLE_UMBRELLA_IDS]
        items.append({
            "rank": i + 1,
            "title": e.get("name"),
            "artist": e.get("artistName"),
            "genres": [g.get("name") for g in genres if g.get("name")],
            "genre_ids": [str(g["genreId"]) for g in genres if g.get("genreId")],
            "released": e.get("releaseDate"),
            "window": _window(e.get("releaseDate"), today),
        })
    if not items:
        return FeedResult("apple", "lagging", error="empty feed")
    return FeedResult("apple", "lagging", items=items)
