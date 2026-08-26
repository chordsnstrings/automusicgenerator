"""Google Trends — daily trending searches by region.

The most leading source in the stack, and the one people most often get wrong.
This uses the public RSS endpoint, not ``pytrends``: the scraper library is
unofficial, aggressively rate-limited, and breaks without notice. The feed
below needs no key and has been stable for years.

Not music-specific, which is the point — it catches a mood before anyone has
written a song about it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ..http import request
from . import FeedResult

URL = "https://trends.google.com/trending/rss"
NS = {"ht": "https://trends.google.com/trending/rss"}


def fetch(region: str = "US", limit: int = 25) -> FeedResult:
    try:
        resp = request("GET", URL, provider="gtrends", params={"geo": region},
                       timeout=30.0, attempts=2)
        if resp.status_code >= 400:
            return FeedResult("gtrends", "leading", error=f"HTTP {resp.status_code}")
        root = ET.fromstring(resp.text)
    except Exception as exc:
        return FeedResult("gtrends", "leading", error=str(exc)[:200])

    items = []
    for node in root.iterfind(".//item"):
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        traffic = (node.findtext("ht:approx_traffic", namespaces=NS) or "").strip()
        headlines = [
            (n.findtext("ht:news_item_title", namespaces=NS) or "").strip()
            for n in node.iterfind("ht:news_item", NS)
        ]
        items.append({
            "term": title,
            "traffic": _traffic_to_int(traffic),
            "context": [h for h in headlines if h][:3],
        })
        if len(items) >= limit:
            break

    if not items:
        return FeedResult("gtrends", "leading", error="feed parsed but empty")
    return FeedResult("gtrends", "leading", items=items)


def _traffic_to_int(raw: str) -> int:
    """'200K+' -> 200000. Best effort; ordering matters more than precision."""
    m = re.match(r"([\d,.]+)\s*([KMB]?)", raw.replace(" ", ""), re.I)
    if not m:
        return 0
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    return int(n * {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()])
