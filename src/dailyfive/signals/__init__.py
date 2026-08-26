"""The Scout's raw inputs: seven free feeds, collected independently.

Every collector returns a :class:`FeedResult` and never raises. A dead source
narrows the evidence and says so in ``error``; it does not take the run down.
That matters because several of these are unauthenticated public endpoints
with no uptime guarantee whatsoever.

Sources are tagged by ``lead`` — how far ahead of the market the signal sits.
Weighting by that tag is the Scout's actual job; a chart position tells you
what already happened.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(slots=True)
class FeedResult:
    source: str
    lead: str                      # "leading" | "moderate" | "lagging"
    items: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.items)

    def summary(self) -> str:
        if self.error:
            return f"{self.source}: unavailable ({self.error})"
        return f"{self.source} ({self.lead}): {len(self.items)} items"


def collect_all(region: str = "US", *, timeout: float = 45.0) -> list[FeedResult]:
    """Fetch every feed concurrently. Slowest feed sets the wall clock."""
    from . import apple, deezer, genius, gtrends, lastfm, reddit, youtube

    collectors: list[tuple[str, Callable[[], FeedResult]]] = [
        ("gtrends", lambda: gtrends.fetch(region)),
        ("reddit", reddit.fetch),
        ("genius", genius.fetch),
        ("youtube", lambda: youtube.fetch(region)),
        ("lastfm", lastfm.fetch),
        ("deezer", deezer.fetch),
        ("apple", lambda: apple.fetch(region)),
    ]

    results: list[FeedResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(collectors)) as pool:
        futures = {pool.submit(fn): name for name, fn in collectors}
        for fut in concurrent.futures.as_completed(futures, timeout=timeout):
            name = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # a collector that raises is a bug, not an outage
                log.exception("collector %s raised", name)
                results.append(FeedResult(source=name, lead="unknown", error=str(exc)))

    order = {"leading": 0, "moderate": 1, "lagging": 2, "unknown": 3}
    results.sort(key=lambda r: (order.get(r.lead, 9), r.source))
    return results
