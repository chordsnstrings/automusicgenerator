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


# ── the two lagging feeds, which are the only genre evidence there is ────────
class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


APPLE_PAYLOAD = {"feed": {"results": [
    # Dolly Parton's "Jolene" is the real row this fixture exists for: a 1973
    # recording riding a news cycle, counted as current genre evidence by any
    # reader that does not look at releaseDate.
    {"name": "Jolene", "artistName": "Dolly Parton", "releaseDate": "1973-10-15",
     "genres": [{"genreId": "6", "name": "Country"},
                {"genreId": "34", "name": "Music"}]},
    {"name": "New Thing", "artistName": "Someone", "releaseDate": "2026-06-01",
     "genres": [{"genreId": "14", "name": "Pop"},
                {"genreId": "34", "name": "Music"}]},
    {"name": "No Date", "artistName": "Nobody", "releaseDate": None,
     "genres": [{"genreId": "14", "name": "Pop"}]},
]}}


def test_apple_keeps_the_genre_id_and_drops_the_umbrella(monkeypatch):
    """genreId 34 is "Music" and sits on 50 of 50 entries in every region. It is
    the root of Apple's genre tree, and counted it wins every chart."""
    from dailyfive.signals import apple

    monkeypatch.setattr(apple, "request", lambda *a, **kw: _Resp(APPLE_PAYLOAD))
    items = apple.fetch("US").items
    assert [i["genre_ids"] for i in items] == [["6"], ["14"], ["14"]]
    assert all("Music" not in i["genres"] for i in items)


def test_apple_partitions_on_release_date_and_never_blends(monkeypatch):
    """Blended, the US chart says country beats pop 23-6; split on releaseDate,
    current releases are country 6 pop 6. The blended number is the one the
    Scout used to see."""
    from dailyfive import genres
    from dailyfive.signals import apple

    monkeypatch.setattr(apple, "request", lambda *a, **kw: _Resp(APPLE_PAYLOAD))
    feed = apple.fetch("US")
    assert [i["window"] for i in feed.items] == ["catalogue", "current", None]

    counts = genres.external_counts([feed])
    assert counts["current"] == {"pop": 1}
    assert counts["catalogue"] == {"country": 1}
    assert "total" not in counts and "blended" not in counts
    # An entry with no usable date belongs to neither scope and is reported.
    assert counts["entries"]["apple_undated"] == 1


DEEZER_CHART = {"data": [
    {"id": 1, "title_short": "Boston", "position": 1, "duration": 190,
     "artist": {"name": "STELLA LEFTY"}, "album": {"id": 900}},
    {"id": 2, "title_short": "Dracula", "position": 2, "duration": 210,
     "artist": {"name": "Tame Impala"}, "album": {"id": 901}},
]}
DEEZER_ALBUMS = {
    900: {"genres": {"data": [{"id": 84, "name": "Country"},
                              {"id": 132, "name": "Pop"}]}},
    901: {"genres": {"data": [{"id": 85, "name": "Alternative"},
                              {"id": 87, "name": "Indie Rock"},
                              {"id": 999, "name": "Ambient Choral"}]}},
}


def _deezer_request(method, url, **kw):
    if url.endswith("/chart/0/tracks"):
        return _Resp(DEEZER_CHART)
    return _Resp(DEEZER_ALBUMS[int(url.rsplit("/", 1)[1])])


def test_deezer_spends_its_enrichment_calls_on_genre_not_bpm(monkeypatch):
    """The chart row's bpm came back 0 for every track on every day checked —
    Deezer's audio analysis lags ingestion, so the field is empty precisely on
    the new releases the feed exists to characterise. Genre is on the album,
    whose id the chart row already carries, so it is the same request budget."""
    from dailyfive.signals import deezer

    calls = []

    def spy(method, url, **kw):
        calls.append(url)
        return _deezer_request(method, url, **kw)

    monkeypatch.setattr(deezer, "request", spy)
    items = deezer.fetch(limit=2).items
    assert not any("/track/" in url for url in calls)
    assert sum("/album/" in url for url in calls) == 2
    assert items[0]["genres"] == ["Country", "Pop"]
    assert all("bpm" not in i for i in items)


def test_a_deezer_sub_genre_rolls_up_and_an_unknown_id_is_reported_not_guessed(monkeypatch):
    """Album objects carry sub-genres the 28-entry /genre list does not, and
    /genre/{id} exposes no parent, so the rollup is a hand-written map. An id
    that is not in it says this file is missing a row, which is a different
    fact from a genre the roster deliberately excludes."""
    from dailyfive import genres
    from dailyfive.signals import deezer

    monkeypatch.setattr(deezer, "request", _deezer_request)
    counts = genres.external_counts([deezer.fetch(limit=2)])
    assert counts["deezer"] == {"alternative": 1, "country": 1, "pop": 1, "rock": 1}
    assert counts["unrecognised"]["deezer"] == {"Ambient Choral (999)": 1}
    assert counts["entries"]["deezer"] == 2


def test_the_two_charts_disagree_and_both_counts_survive(monkeypatch):
    """Apple says country, Deezer says pop, on the same day. Reporting one
    number would have to pick, and the disagreement is the informative part."""
    from dailyfive import genres
    from dailyfive.signals import apple, deezer

    monkeypatch.setattr(apple, "request", lambda *a, **kw: _Resp(APPLE_PAYLOAD))
    monkeypatch.setattr(deezer, "request", _deezer_request)
    counts = genres.external_counts([apple.fetch("US"), deezer.fetch(limit=2)])
    assert counts["catalogue"]["country"] == 1
    assert counts["deezer"]["pop"] == 1
    assert counts["sources"] == ["apple", "deezer"]
