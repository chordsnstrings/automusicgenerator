"""A dead feed must narrow the evidence, never take the run down."""

import pytest

from dailyfive.signals import FeedResult, collect_all
from dailyfive.signals.gtrends import _traffic_to_int


def test_feed_result_reports_its_own_health():
    ok = FeedResult("x", "leading", items=[{"a": 1}])
    dead = FeedResult("y", "lagging", error="HTTP 500")
    empty = FeedResult("z", "moderate", items=[])
    assert ok.ok and not dead.ok and not empty.ok
    assert "unavailable" in dead.summary()


@pytest.mark.network
def test_collect_all_never_raises_even_with_a_broken_collector(monkeypatch):
    from dailyfive.signals import apple

    def boom(*a, **kw):
        raise RuntimeError("upstream on fire")

    monkeypatch.setattr(apple, "fetch", boom)
    results = collect_all("US", timeout=60)
    assert len(results) == 7
    assert any(r.error and "on fire" in r.error for r in results)


@pytest.mark.network
def test_results_are_ordered_by_how_leading_they_are():
    from dailyfive.signals import collect_all
    results = collect_all("US", timeout=60)
    order = {"leading": 0, "moderate": 1, "lagging": 2, "unknown": 3}
    ranks = [order.get(r.lead, 9) for r in results]
    assert ranks == sorted(ranks)


@pytest.mark.network
def test_keyless_feeds_work_with_no_credentials_at_all():
    """Google Trends, Deezer and Apple need no keys — that claim is load-bearing."""
    results = {r.source: r for r in collect_all("US", timeout=60)}
    keyless = [results[s] for s in ("gtrends", "deezer", "apple") if s in results]
    assert any(r.ok for r in keyless), \
        "no keyless feed returned data; the free-tier claim would be false"


@pytest.mark.network
def test_keyed_feeds_report_the_missing_key_by_name():
    results = {r.source: r for r in collect_all("US", timeout=60)}
    for source, var in (("youtube", "YOUTUBE_API_KEY"), ("lastfm", "LASTFM_API_KEY"),
                        ("genius", "GENIUS_ACCESS_TOKEN")):
        r = results.get(source)
        if r and not r.ok and "not set" in (r.error or ""):
            assert var in r.error


def test_traffic_parsing():
    assert _traffic_to_int("200K+") == 200_000
    assert _traffic_to_int("1M+") == 1_000_000
    assert _traffic_to_int("5,000+") == 5_000
    assert _traffic_to_int("") == 0
