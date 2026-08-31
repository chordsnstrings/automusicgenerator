"""The genre record — and above all, that it can actually accumulate.

The whole reason this module exists is that six months of style strings would
teach the studio nothing about genre. ``test_the_genre_signal_actually_
accumulates`` is the test that pins that down; everything else here defends the
conditions it needs.
"""

from datetime import date, timedelta

import pytest

from dailyfive import genres
from dailyfive.archivist import MIN_OBSERVATIONS, _style_tokens
from dailyfive.conductor import Conductor
from dailyfive.db import session_scope
from dailyfive.genres import (ALIASES, APPLE_GENRE_IDS, APPLE_OUTSIDE_ROSTER,
                              APPLE_UMBRELLA_IDS, DEEZER_GENRE_IDS, FAMILIES,
                              GENRE_MAX_PER_FAMILY, GENRE_MIN_RATED,
                              GENRE_WARM_FAMILIES, GENRE_WARM_TOTAL, ID3_NAME,
                              SPECIFICS, VOCABULARY)
from dailyfive.models import (Brief, Clip, Job, JobState, Outcome, Run,
                              RunPhase, SlotType, utcnow)


# ── the frozen record ────────────────────────────────────────────────────────
# A label that has shipped is never renamed and never removed — renaming
# orphans every row carrying the old value and splits one track record into two
# halves, neither of which clears the sample threshold.
#
# Nothing has shipped under any of these yet: the six briefs and twelve clips in
# production predate genre being recorded and keep a NULL by decision, because a
# model inferring their genre from style strings after the fact is exactly the
# fabricated track record this codebase forbids. The set is frozen at the commit
# that introduced the vocabulary anyway. Within a fortnight most of it will have
# been briefed, and the cost of freezing early is that deleting a never-used
# label needs a deliberate edit here; the cost of freezing late is an orphaned
# track record nobody notices.
FROZEN_FAMILIES = frozenset({
    "pop", "country", "hip-hop", "r-and-b", "rock",
    "alternative", "electronic", "folk", "latin",
})
FROZEN_SPECIFICS = frozenset({
    "alt-pop", "synth-pop", "dance-pop", "bedroom-pop",
    "country-folk", "country-pop", "country-soul", "country-trap",
    "outlaw-country",
    "trap", "boom-bap", "cloud-rap", "drill",
    "alt-r-and-b", "neo-soul", "contemporary-r-and-b", "gospel-soul",
    "indie-rock", "garage-rock", "heartland-rock", "post-punk",
    "dream-pop", "shoegaze", "art-rock", "emo",
    "house", "ambient-electronic", "synthwave", "uk-garage",
    "indie-folk", "singer-songwriter", "americana", "chamber-folk",
    "reggaeton", "latin-pop", "bachata", "cumbia",
})

# The five lead style fragments actually shipped on 2026-08-26, quoted from the
# meta.json of the day. These are what the ID3 genre tag was cut from.
SHIPPED_LEAD_FRAGMENTS = [
    "slow country-folk ballad",
    "midtempo indie-folk with ambient electronics",
    "country-soul hybrid at the uptempo edge of midtempo",
    "midtempo country-trap",
    "uptempo alt-R&B at the low end of uptempo",
]


# ── helpers ──────────────────────────────────────────────────────────────────
def _run(day: date) -> int:
    with session_scope() as s:
        r = Run(run_date=day, phase=RunPhase.SHIPPED)
        s.add(r)
        s.flush()
        return r.id


def _brief(run_id: int, idx: int, family: str | None, specific: str | None = None) -> int:
    with session_scope() as s:
        b = Brief(run_id=run_id, slot_type=SlotType.FULL, idx=idx,
                  title=f"S{run_id}-{idx}", theme="a specific situation",
                  genre_family=family, genre=specific,
                  style_string="close-mic vocal, brushed kit")
        s.add(b)
        s.flush()
        return b.id


def _clips(run_id, brief_id, *, n=2, qc="pass", score=6.0, shipped=0):
    """Two clips per brief, carrying the brief's genre, as the Conductor does."""
    ids = []
    with session_scope() as s:
        brief = s.get(Brief, brief_id)
        job = Job(run_id=run_id, brief_id=brief_id,
                  idempotency_key=f"k{run_id}:{brief_id}", state=JobState.MIRRORED)
        s.add(job)
        s.flush()
        for i in range(n):
            c = Clip(run_id=run_id, job_id=job.id, brief_id=brief_id,
                     audio_id=f"a{run_id}-{brief_id}-{i}", variant=i,
                     slot_type=SlotType.FULL, theme=brief.theme,
                     genre_family=brief.genre_family, genre=brief.genre,
                     style_string=brief.style_string, qc_verdict=qc,
                     score_total=score, shipped=i < shipped)
            s.add(c)
            s.flush()
            ids.append(c.id)
    return ids


def _rate(clip_id: int, rating: int, *, days_ago: int = 0) -> None:
    with session_scope() as s:
        s.add(Outcome(clip_id=clip_id, rating=rating,
                      rated_at=utcnow() - timedelta(days=days_ago)))


def _stock(family: str, *, n: int, rating: int, start: int = 900, qc="pass",
           score=6.0, specific: str | None = None) -> None:
    """n rated briefs in one family, one per day so no day can carry two."""
    for i in range(n):
        rid = _run(date(2026, 1, 1) + timedelta(days=start + i))
        bid = _brief(rid, 0, family, specific)
        clips = _clips(rid, bid, qc=qc, score=score, shipped=1)
        _rate(clips[0], rating)


# ── vocabulary integrity ─────────────────────────────────────────────────────
def test_every_specific_maps_to_a_real_family():
    assert set(SPECIFICS.values()) <= set(FAMILIES)
    assert set(SPECIFICS.values()) == set(FAMILIES), "a family with no specifics"


def test_no_label_appears_in_two_families():
    assert len(FAMILIES) == len(set(FAMILIES))
    assert not set(FAMILIES) & set(SPECIFICS), "a label that is both a family and a specific"
    assert sum(len(v) for v in VOCABULARY.values()) == len(SPECIFICS)


def test_every_family_has_an_id3_name():
    assert set(ID3_NAME) == set(FAMILIES)
    for family in FAMILIES:
        name = genres.id3_name(family)
        assert name and "," not in name and len(name) <= 24


def test_every_apple_and_deezer_id_maps_into_the_roster():
    assert set(APPLE_GENRE_IDS.values()) <= set(FAMILIES)
    assert set(DEEZER_GENRE_IDS.values()) <= set(FAMILIES)
    assert not set(APPLE_GENRE_IDS) & set(APPLE_OUTSIDE_ROSTER)
    assert not set(APPLE_GENRE_IDS) & APPLE_UMBRELLA_IDS


def test_the_umbrella_genre_is_dropped():
    """genreId 34 is on 50 of 50 entries in every region — a parent, not a genre."""
    for umbrella in APPLE_UMBRELLA_IDS:
        assert genres.apple_family(umbrella) is None
        assert genres.apple_family(int(umbrella)) is None


def test_an_outside_id_is_recognised_rather_than_guessed():
    assert genres.apple_family("22") is None          # Christian
    assert APPLE_OUTSIDE_ROSTER["22"] == "christian"
    assert genres.apple_family("99999") is None       # never seen at all


def test_the_id_maps_accept_either_a_string_or_an_int():
    """Apple sends strings, Deezer sends ints. A dialect difference at the
    boundary must not silently miss every row."""
    assert genres.apple_family("6") == genres.apple_family(6) == "country"
    assert genres.deezer_family("132") == genres.deezer_family(132) == "pop"


def test_shipped_labels_are_never_renamed():
    assert FROZEN_FAMILIES <= set(FAMILIES), "a family was renamed or removed"
    assert FROZEN_SPECIFICS <= set(SPECIFICS), "a specific was renamed or removed"


def test_the_family_level_fits_the_sample_budget():
    """Five rateable songs a day over a 60-day record is 300 rated briefs, so
    300 / GENRE_MIN_RATED labels is the ceiling at perfect rating coverage.

    The assertion is on FAMILIES alone, and the specific level is deliberately
    not held to it. This test once asserted both, which read as though the two
    were reconciled when they were only equal — there were exactly 37 specifics
    and the ceiling was exactly 37. At the real rating coverage (a third of
    shipped songs) about a dozen specifics could ever rank, so the specific
    level was already above its budget before a single fusion was added.

    The two levels do different jobs. The family is what is learned. The
    specific is vocabulary the Director writes prompts with, and a label that
    never ranks still earns its place by being reachable — which is the whole
    reason "electronic-hip-hop" exists as a term at all.
    """
    ceiling = 60 * 5 // GENRE_MIN_RATED
    assert len(FAMILIES) * 4 <= ceiling, "too many families to sustain a rated signal"


def test_an_unranked_specific_is_never_reported_as_ranked():
    """The safeguard that lets the specific level exceed its sample budget: a
    label below GENRE_MIN_RATED reports no taste at all, so a vocabulary far
    larger than the ceiling cannot manufacture a track record."""
    data = genres.scores()
    for row in data["specifics"].values():
        if row["rated_n"] < GENRE_MIN_RATED:
            assert row["taste"] is None


def test_the_thresholds_are_stricter_than_the_style_token_floor():
    """A genre decision reallocates the whole day's irreversible budget; a style
    token only decides whether a phrase appears in a prompt."""
    assert GENRE_MIN_RATED > MIN_OBSERVATIONS


# ── labelling ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("family,specific", [
    ("country", "country-folk"),
    ("Country", "Country Folk"),
    ("  COUNTRY  ", "country_folk"),
    ("country", "Country-Folk"),
])
def test_a_spelling_variant_is_the_same_label(family, specific):
    """"country-folk" and "country folk" landing as two labels with no history
    each is precisely how the studio has learned nothing about genre so far."""
    assert genres.normalise(family, specific) == ("country", "country-folk")


def test_an_alias_is_the_same_genre_not_a_new_one():
    assert genres.normalise("R&B", "alt R&B") == ("r-and-b", "alt-r-and-b")
    assert genres.normalise("Rap", None) == ("hip-hop", None)
    assert set(ALIASES.values()) <= set(FAMILIES) | set(SPECIFICS)


def test_an_off_vocabulary_genre_normalises_to_nulls():
    assert genres.normalise("jazz", "bebop") == (None, None)
    assert genres.normalise(None, None) == (None, None)
    assert genres.normalise(7, ["country"]) == (None, None)


def test_the_specific_wins_when_it_disagrees_with_the_family():
    """The specific is the more specific claim and its family is unambiguous."""
    assert genres.normalise("r-and-b", "country-soul") == ("country", "country-soul")


def test_a_family_with_an_unknown_specific_keeps_the_family():
    assert genres.normalise("country", "yodel-core") == ("country", None)


def test_id3_name_is_a_player_genre_not_our_database_key():
    assert genres.id3_name("r-and-b") == "R&B"
    assert genres.id3_name("hip-hop") == "Hip-Hop"
    assert genres.id3_name(None) is None
    assert genres.id3_name("jazz") is None


# ── the canary ───────────────────────────────────────────────────────────────
class _FakeSuno:
    def generate(self, payload):
        return "task-1"

    def credits(self):
        return 1000

    def record_info(self, task_id):
        return {"status": "PENDING"}


def _deliver(run_id: int, brief_id: int) -> list[int]:
    """Two clips through the real Conductor, so the copy under test is the
    copy production performs and not one this test performs for it."""
    with session_scope() as s:
        job = Job(run_id=run_id, brief_id=brief_id,
                  idempotency_key=f"c{run_id}:{brief_id}",
                  payload={"model": "V5_5", "style": "close-mic vocal"},
                  state=JobState.QUEUED)
        s.add(job)
        s.flush()
        job_id = job.id
    Conductor(run_id, client=_FakeSuno()).ingest_record(job_id, {
        "status": "SUCCESS", "response": {"sunoData": [
            {"id": f"au-{job_id}-{i}", "audio_url": f"https://x/{job_id}-{i}.mp3",
             "title": f"T{i}", "duration": 190.0} for i in range(2)]}})
    with session_scope() as s:
        return [c.id for c in s.query(Clip).filter(Clip.job_id == job_id)
                .order_by(Clip.variant).all()]


def test_the_genre_signal_actually_accumulates():
    """Fourteen simulated days must leave at least three families ranked.

    This is the regression test for the failure the whole change exists to fix.
    It fails on any change that reintroduces per-string keying, drops the copy
    in the Conductor, grows the vocabulary past the sample budget, or stops the
    allocator spreading.
    """
    taste = {"pop": 8, "country": 9, "hip-hop": 6, "r-and-b": 7, "rock": 5,
             "alternative": 6, "electronic": 4, "folk": 8, "latin": 7}

    for day in range(14):
        rows = genres.slate(7)
        assert sum(r["specs"] for r in rows) == 7
        allocated = [r["genre_family"] for r in rows for _ in range(r["specs"])]
        run_id = _run(date(2026, 2, 1) + timedelta(days=day))
        for idx, family in enumerate(allocated):
            bid = _brief(run_id, idx, family, VOCABULARY[family][0])
            clip_ids = _deliver(run_id, bid)
            if idx < 5:                       # five of seven ship, and get rated
                with session_scope() as s:
                    s.get(Clip, clip_ids[0]).shipped = True
                _rate(clip_ids[0], taste[family])

    data = genres.scores()
    ranked = [f for f, row in data["families"].items()
              if row["rated_n"] >= GENRE_MIN_RATED and row["taste"] is not None]
    assert len(ranked) >= 3, f"only {len(ranked)} families ranked after 14 days"
    assert genres.status(data=data)["regime"] in ("thin", "warm")


def test_free_prose_style_strings_never_accumulate():
    """The paired negative, and the reason the column exists.

    Not a bug to fix in the tokeniser. Across a whole day of five songs, no
    genre-bearing fragment occurs twice — every one is prose written for one
    song — so nothing genre-shaped can ever be counted more than once a day,
    while the tokens that do recur are short mix-note stubs like "no synths".
    """
    counts: dict[str, int] = {}
    for fragment in SHIPPED_LEAD_FRAGMENTS:
        for token in _style_tokens(fragment):
            counts[token] = counts.get(token, 0) + 1

    genre_bearing = [tok for tok in counts
                     if any(f.split("-")[0] in tok for f in FAMILIES)]
    assert genre_bearing, "the fixture no longer contains genre words"
    assert max(counts.values()) == 1, (
        "a fragment repeated within one day — the fixture no longer pins the defect")


def _two_briefs_rated(style: str) -> int:
    """Two briefs, four clip rows, every one rated. Returns the run id."""
    rid = _run(date.today())
    for idx in range(2):
        with session_scope() as s:
            b = Brief(run_id=rid, slot_type=SlotType.FULL, idx=idx,
                      title=f"D{idx}", theme="x", genre_family="country",
                      genre="country-soul", style_string=style)
            s.add(b)
            s.flush()
            bid = b.id
        for cid in _clips(rid, bid, shipped=2):
            _rate(cid, 9)
    return rid


def test_two_briefs_are_four_clip_rows_and_that_is_how_the_codex_was_taught():
    """The doubling, stated as a test because it is the actual defect.

    ``archivist.aggregate`` counts clip ROWS, and one brief returns two clips
    carrying identical brief-derived fields — so two briefs look like four
    observations and clear MIN_OBSERVATIONS on one day's noise. That is how the
    studio's entire learned style vocabulary came to be two entries off two
    briefs. The genre columns are counted over distinct briefs instead, so the
    same two briefs stay unranked.
    """
    from dailyfive.archivist import aggregate

    _two_briefs_rated("brushed drums, upright bass")
    assert aggregate()["style_scores"].get("brushed drums") is not None, (
        "the doubling is gone from the aggregator — update this test, not the fixture")
    assert genres.scores()["families"]["country"]["taste"] is None


def test_a_negation_never_becomes_something_observed_to_score_well():
    """The live codex reached v3 with a learned table of exactly "no synths"
    6.11 and "no pads" 6.11, rendered into the Music Director's prompt under a
    heading saying they were observed to score well, beside an instruction
    saying such observations beat its priors. An absence cannot be credited
    with an outcome in either direction, so it is dropped where it is collected
    and again where it is rendered.
    """
    from dailyfive.archivist import _style_tokens, aggregate
    from dailyfive.codex import Codex, is_negation

    assert _style_tokens("no synths, no pads, brushed drums") == ["brushed drums"]
    assert is_negation("without strings") and not is_negation("nocturnal pads")

    _two_briefs_rated("no synths, no pads")
    assert aggregate()["style_scores"] == {}

    # And the two already sitting in the live codex are not rendered either.
    stale = Codex(version=3, body={"learned": {"style_scores": {"no synths": 6.11},
                                               "avoid": ["no pads"]}}, personas=[])
    context = stale.brief_context()
    assert "no synths" not in context and "no pads" not in context


def test_the_char_cap_drops_the_genre_fragment():
    """Three of the five shipped lead fragments are over the cap at 44, 51 and
    41 characters, so the tokeniser keeps the short stubs and drops the
    qualified genre phrases. Documented here so nobody "fixes" it by raising
    the cap, which would only make the no-repeats problem larger.
    """
    over = [f for f in SHIPPED_LEAD_FRAGMENTS if len(f) > 40]
    assert len(over) == 3
    assert sorted(len(f) for f in over) == [41, 44, 51]
    for fragment in over:
        assert _style_tokens(fragment) == []


def test_the_conductor_copies_the_genre_onto_the_clip(run_id, brief_factory):
    bid = brief_factory(genre_family="country", genre="country-soul")
    clip_ids = _deliver(run_id, bid)
    with session_scope() as s:
        for cid in clip_ids:
            clip = s.get(Clip, cid)
            assert (clip.genre_family, clip.genre) == ("country", "country-soul")


# ── regimes and honesty ──────────────────────────────────────────────────────
def test_a_fresh_install_reports_cold_and_calls_it_no_preference():
    st = genres.status()
    assert st["regime"] == "cold"
    assert "coverage" in st["note"] and "preference" in st["note"]
    assert st["ranked"] == []


def test_a_family_below_the_threshold_reports_no_mean():
    _stock("country", n=GENRE_MIN_RATED - 1, rating=9)
    row = genres.scores()["families"]["country"]
    assert row["rated_n"] == GENRE_MIN_RATED - 1
    assert row["taste"] is None

    _stock("country", n=1, rating=9, start=800)
    row = genres.scores()["families"]["country"]
    assert row["rated_n"] == GENRE_MIN_RATED
    assert row["taste"] == pytest.approx(9.0, abs=0.05)


def test_no_family_reports_a_number_before_the_bar_is_cleared():
    for family in ("country", "pop", "folk"):
        _stock(family, n=3, rating=9, start=700 + 20 * FAMILIES.index(family))
    data = genres.scores()
    assert data["ranked"] == []
    assert all(row["taste"] is None for row in data["families"].values())


def test_one_day_of_briefs_cannot_promote_a_family():
    """Two briefs in one day are four clip rows. That is how "no synths"
    reached the codex, and a controlled label repeats by design."""
    rid = _run(date(2026, 3, 1))
    for idx in range(2):
        bid = _brief(rid, idx, "country", "country-soul")
        for cid in _clips(rid, bid, shipped=2):
            _rate(cid, 10)
    row = genres.scores()["families"]["country"]
    assert row["rated_n"] == 2, "clips were counted as independent decisions"
    assert row["taste"] is None


def test_scores_count_distinct_briefs_not_clips():
    rid = _run(date(2026, 3, 2))
    bid = _brief(rid, 0, "folk", "indie-folk")
    clips = _clips(rid, bid, shipped=2)
    _rate(clips[0], 9)
    _rate(clips[1], 3)
    row = genres.scores()["families"]["folk"]
    assert row["rated_n"] == 1
    assert row["clips"] == 2
    assert row["taste_raw"] == pytest.approx(6.0)   # the pair is one decision


def test_score_trend_is_excluded_from_taste():
    """Trend scores fit to the signal sheet, and the signal sheet chose the
    genre. A loop with no external input in it is not evidence."""
    rid = _run(date(2026, 3, 3))
    bid = _brief(rid, 0, "electronic", "house")
    with session_scope() as s:
        for clip_id in _clips(rid, bid):
            clip = s.get(Clip, clip_id)
            clip.score_trend = 10.0
            clip.score_hook = 10.0
            clip.score_total = 10.0
    row = genres.scores()["families"]["electronic"]
    assert row["taste"] is None and row["taste_raw"] is None
    assert row["producer"] == pytest.approx(10.0)


def test_the_producer_score_never_becomes_a_taste_score():
    _stock("rock", n=GENRE_MIN_RATED, rating=3, score=10.0)
    row = genres.scores()["families"]["rock"]
    assert row["taste"] == pytest.approx(3.0, abs=0.05)
    assert row["producer"] == pytest.approx(10.0)


def test_a_qc_failure_does_not_lower_a_genre_taste_score():
    """The "Still (Nursery Without a Name)" case: a truncated render is a
    supplier fact. It must move reliability, not taste."""
    _stock("latin", n=GENRE_MIN_RATED, rating=8, qc="fail", start=600)
    row = genres.scores()["families"]["latin"]
    assert row["taste"] == pytest.approx(8.0, abs=0.05)
    assert row["reliability"] == 0.0


def test_reliability_is_available_before_any_rating_exists():
    rid = _run(date(2026, 3, 4))
    bid = _brief(rid, 0, "pop", "alt-pop")
    _clips(rid, bid, qc="fail")
    row = genres.scores()["families"]["pop"]
    assert row["taste"] is None
    assert row["reliability"] == 0.0


def test_every_family_appears_even_with_nothing_behind_it():
    """Day one the page is the vocabulary, not the scoreboard."""
    data = genres.scores()
    assert set(data["families"]) == set(FAMILIES)
    assert all(row["briefed"] == 0 for row in data["families"].values())


def test_briefs_that_predate_the_column_are_counted_as_unlabelled():
    rid = _run(date(2026, 3, 5))
    _brief(rid, 0, None, None)
    data = genres.scores()
    assert data["unlabelled_briefs"] == 1
    assert data["briefed_families"] == []


def test_an_old_rating_weighs_less_than_a_fresh_one():
    rid = _run(date(2026, 3, 6))
    for idx, (rating, age) in enumerate([(10, 0), (2, 900)]):
        bid = _brief(rid + 0, idx, "alternative", "shoegaze")
        clips = _clips(rid, bid)
        _rate(clips[0], rating, days_ago=age)
    row = genres.scores()["families"]["alternative"]
    assert row["taste_raw"] > 6.0, "a two-and-a-half-year-old rating still weighs full"


def test_the_specific_level_reports_its_own_row():
    _stock("country", n=2, rating=9, specific="country-trap")
    data = genres.scores()
    assert data["specifics"]["country-trap"]["rated_n"] == 2
    assert data["specifics"]["country-trap"]["taste"] is None
    assert data["families"]["country"]["rated_n"] == 2


# ── allocation ───────────────────────────────────────────────────────────────
def test_cold_start_is_round_robin():
    rows = genres.slate(7, rhythm_floor=0)
    assert len(rows) == 7
    assert all(r["specs"] == 1 for r in rows)
    assert len({r["genre_family"] for r in rows}) == 7
    assert all(r["mean"] is None and r["n"] == 0 for r in rows)
    assert all(r["stance"] == "explore" for r in rows)


def test_the_slate_is_deterministic():
    _stock("country", n=GENRE_MIN_RATED, rating=9)
    _stock("pop", n=GENRE_MIN_RATED, rating=5, start=500)
    first = genres.slate(7)
    second = genres.slate(7)
    assert first == second, "identical inputs produced a different slate"


def test_the_printed_arithmetic_adds_up():
    """The console sentence has to be checkable, not decorative."""
    _stock("country", n=GENRE_MIN_RATED, rating=9)
    _stock("pop", n=GENRE_MIN_RATED, rating=5, start=500)
    _stock("folk", n=GENRE_MIN_RATED, rating=7, start=300)
    for row in genres.slate(7):
        if row["ucb"] is None:
            assert row["mean_scaled"] is None or row["bonus"] is None
            continue
        assert abs(row["mean_scaled"] + row["bonus"] - row["ucb"]) < 1e-9
        assert row["mean_scaled"] == pytest.approx((row["mean"] - 1) / 9, abs=1e-6)


def test_no_family_takes_more_than_two_of_seven():
    _stock("country", n=40, rating=10)
    for row in genres.slate(7):
        assert row["specs"] <= GENRE_MAX_PER_FAMILY


def test_the_explore_floor_survives_a_dominant_family():
    """The loop-breaker. Nothing external ever contradicts the studio's own
    preference, so the floor is what keeps the circuit open."""
    _stock("country", n=40, rating=10)
    _stock("pop", n=GENRE_MIN_RATED, rating=5, start=500)
    _stock("folk", n=GENRE_MIN_RATED, rating=5, start=300)
    assert genres.status()["regime"] == "warm"

    rows = genres.slate(7, explore_briefs=2)
    scored = genres.scores()["families"]
    unproven = sum(r["specs"] for r in rows
                   if scored[r["genre_family"]]["rated_n"] < GENRE_MIN_RATED)
    assert unproven >= 2, "a dominant family swallowed the exploration floor"
    taken = {r["genre_family"]: r["specs"] for r in rows}
    assert taken.get("country", 0) <= GENRE_MAX_PER_FAMILY


def test_the_explore_floor_is_read_from_config_not_hardcoded(monkeypatch):
    _stock("country", n=40, rating=10)
    _stock("pop", n=GENRE_MIN_RATED, rating=5, start=500)
    _stock("folk", n=GENRE_MIN_RATED, rating=5, start=300)
    monkeypatch.setenv("GENRE_EXPLORE_BRIEFS", "4")
    from dailyfive.config import reload_settings
    reload_settings()
    rows = genres.slate(7)
    scored = genres.scores()["families"]
    unproven = sum(r["specs"] for r in rows
                   if scored[r["genre_family"]]["rated_n"] < GENRE_MIN_RATED)
    assert unproven >= 4


def test_the_thin_regime_moves_one_brief_and_no_more():
    _stock("country", n=GENRE_MIN_RATED, rating=9)
    st = genres.status()
    assert st["regime"] == "thin", st["note"]
    rows = genres.slate(7)
    lead = next(r for r in rows if r["genre_family"] == "country")
    # Coverage alone would give the most-sampled family none of the seven. One
    # is the whole of the thin response: enough that a user who has rated
    # fifteen songs sees the studio move, not enough to act on one ranked
    # family as though it were a ranking.
    assert lead["specs"] == 1
    assert lead["stance"] == "exploit"
    assert sum(r["specs"] for r in rows) == 7
    assert max(r["specs"] for r in rows) == 1


def test_the_thin_note_names_both_bars_it_has_not_cleared():
    _stock("country", n=GENRE_MIN_RATED, rating=9)
    note = genres.status()["note"]
    assert str(GENRE_WARM_FAMILIES) in note and str(GENRE_WARM_TOTAL) in note


def test_a_slate_row_carries_its_sample_count_always():
    """Today's /codex shows "no synths 6.11" with no sample count anywhere on
    the row. That is how a two-decision average comes to look like a finding."""
    _stock("country", n=GENRE_MIN_RATED, rating=9)
    for row in genres.slate(7):
        assert "n" in row and isinstance(row["n"], int)
        assert row["basis"]


def test_an_external_chart_breaks_a_tie_and_never_produces_a_score():
    external = {"current": {"latin": 6, "country": 6}, "catalogue": {"country": 17}}
    # The rhythm floor is stood down: this is a test of the tie-break, and a
    # floor that claims a pick before the charts are read would be testing two
    # mechanisms at once and telling you about neither.
    rows = genres.slate(2, external=external, rhythm_floor=0)
    picked = [r["genre_family"] for r in rows]
    assert "latin" in picked and "country" in picked
    assert all(r["mean"] is None for r in rows)
    assert any("current-release chart" in r["basis"] or
               "current-release chart" in r["why"] for r in rows)


def test_the_catalogue_count_never_reaches_the_allocator():
    """Blended, country beats pop 23-6; split, they are tied 6-6. The blended
    number is the one that lies."""
    named = genres.external_families(None, {"current": {"pop": 6},
                                            "catalogue": {"country": 17}})
    assert named == {"pop": ["the current-release chart"]}


def test_a_genre_mix_name_off_the_vocabulary_is_ignored_not_guessed():
    named = genres.external_families({"genre_mix": ["Country", "K-Pop", "afrobeats"]}, None)
    assert named == {"country": ["today's genre_mix"]}


def test_a_slate_never_exceeds_or_falls_short_of_the_day():
    _stock("country", n=GENRE_MIN_RATED, rating=9)
    for n in (1, 2, 5, 7, 9):
        assert sum(r["specs"] for r in genres.slate(n)) == n
    assert genres.slate(0) == []


# ── enforcement ──────────────────────────────────────────────────────────────
def test_a_spec_with_an_off_vocabulary_genre_is_kept_with_nulls():
    """A discarded spec costs a whole generation over a label."""
    specs = [{"genre_family": "jazz", "genre": "bebop", "style_string": "x"}]
    ledger = genres.enforce(specs, [{"genre_family": "country", "specs": 7}])
    assert specs[0]["genre_family"] is None and specs[0]["genre"] is None
    assert specs[0]["style_string"] == "x", "the spec itself was altered"
    assert ledger["unlabelled"] == 1
    assert ledger["off_vocabulary"] == ["jazz"]


def test_enforce_normalises_every_label_it_writes():
    specs = [{"genre_family": "Country", "genre": "Country Soul"}]
    genres.enforce(specs, [{"genre_family": "country", "specs": 7}])
    assert specs[0] == {"genre_family": "country", "genre": "country-soul"}


def test_an_off_slate_choice_is_recorded_with_its_reason():
    specs = [{"genre_family": "folk", "genre": "indie-folk",
              "genre_off_slate_reason": "no theme left that country could carry"}]
    ledger = genres.enforce(specs, [{"genre_family": "country", "specs": 7}])
    assert ledger["off_slate"] == [
        {"genre_family": "folk", "reason": "no theme left that country could carry"}]
    assert ledger["got"] == {"folk": 1}


def test_a_breach_of_the_cap_is_recorded_rather_than_relabelled():
    """A persona can be swapped after the fact; a genre is baked into the
    style_string the Director wrote for it."""
    specs = [{"genre_family": "country", "genre": "country-pop"} for _ in range(4)]
    ledger = genres.enforce(specs, [{"genre_family": "country", "specs": 2}])
    assert ledger["over_cap"]["country"] == {"asked": 2, "got": 4, "cap": 2}
    assert all(s["genre_family"] == "country" for s in specs), "a spec was relabelled"


# ── the migration ────────────────────────────────────────────────────────────
def test_existing_rows_survive_the_migration_as_null(run_id, brief_factory):
    """The twelve clips in production predate genre being recorded and keep a
    NULL. No server_default, because a default across those rows would invent a
    track record on day one of the feature that exists to stop inventing them.
    """
    bid = brief_factory()
    rid = run_id
    clip_ids = _clips(rid, bid)
    with session_scope() as s:
        assert s.get(Brief, bid).genre_family is None
        assert s.get(Brief, bid).genre is None
        for cid in clip_ids:
            assert s.get(Clip, cid).genre_family is None
    data = genres.scores()
    assert data["unlabelled_briefs"] == 1
    assert all(row["briefed"] == 0 for row in data["families"].values())


# ── the console's one windowed number ────────────────────────────────────────
def test_a_delta_needs_both_windows_to_clear_the_bar():
    """Half a comparison is not a comparison. Eight rated briefs in the recent
    window and three in the prior one is a number about the recent window only,
    and printing it as a change would be inventing the other half."""
    _stock("country", n=GENRE_MIN_RATED, rating=9, start=900)
    moves = genres.trend()
    assert moves["country"]["recent_n"] == GENRE_MIN_RATED
    assert moves["country"]["prior_n"] == 0
    assert moves["country"]["delta"] is None
    assert moves["country"]["recent"] == pytest.approx(9.0, abs=0.05)


def test_a_delta_is_printed_once_both_windows_are_full():
    n = GENRE_MIN_RATED
    for i in range(n):
        rid = _run(date(2026, 1, 1) + timedelta(days=700 + i))
        bid = _brief(rid, 0, "folk", "indie-folk")
        _rate(_clips(rid, bid, shipped=1)[0], 8, days_ago=2)
    for i in range(n):
        rid = _run(date(2026, 1, 1) + timedelta(days=800 + i))
        bid = _brief(rid, 0, "folk", "indie-folk")
        _rate(_clips(rid, bid, shipped=1)[0], 5, days_ago=20)
    moves = genres.trend()["folk"]
    assert moves["recent_n"] == n and moves["prior_n"] == n
    assert moves["delta"] == pytest.approx(3.0, abs=0.05)


def test_the_delta_counts_distinct_briefs_not_clips():
    """The same rule as everywhere else: two clips of a pair are one decision,
    and letting them count twice would let one day fill a window on its own."""
    rid = _run(date(2026, 1, 1) + timedelta(days=990))
    bid = _brief(rid, 0, "rock", "indie-rock")
    for cid in _clips(rid, bid, shipped=2):
        _rate(cid, 7, days_ago=1)
    assert genres.trend()["rock"]["recent_n"] == 1


def test_the_window_never_reaches_the_allocator():
    """trend() is display only. scores() has no window on purpose — a decay
    instead of a cutoff — and the delta is allowed to exist only because
    nothing that allocates a brief reads it."""
    import inspect

    source = inspect.getsource(genres.slate) + inspect.getsource(genres.scores)
    assert "trend(" not in source


def test_the_thin_regime_reason_counts_the_ranked_families_it_beat():
    """The thin regime holds while EITHER fewer than GENRE_WARM_FAMILIES are
    ranked OR fewer than GENRE_WARM_TOTAL briefs are rated, so three ranked
    families and 24 ratings is thin. The sentence claiming a unique leader is
    printed verbatim in the console's Why picked column, beside a table showing
    all three ranked."""
    _stock("country", n=GENRE_MIN_RATED, rating=9, start=900)
    _stock("pop", n=GENRE_MIN_RATED, rating=6, start=940)
    _stock("rock", n=GENRE_MIN_RATED, rating=4, start=980)
    data = genres.scores()
    assert len(data["ranked"]) == 3
    assert genres.status(data=data)["regime"] == "thin"

    lead = next(r for r in genres.slate(7) if r["genre_family"] == "country")
    assert "the only ranked leader" not in lead["why"]
    assert "highest-scoring of the 3 ranked families" in lead["why"]
    assert "9.0 over 8 rated briefs" in lead["why"]


def test_a_single_ranked_family_still_reads_as_one():
    _stock("country", n=GENRE_MIN_RATED, rating=9)
    lead = next(r for r in genres.slate(7) if r["genre_family"] == "country")
    assert "highest-scoring of the 1 ranked family," in lead["why"]


def test_the_producer_mean_carries_the_clips_it_was_taken_over():
    """It counts CLIPS where every other count on the row counts briefs, and
    two clips of a pair are one decision. A cell that printed the number
    without it would be the bare average this module exists to stop printing."""
    rid = _run(date(2026, 6, 1))
    bid = _brief(rid, 0, "country", "country-soul")
    _clips(rid, bid, n=2, score=9.4)
    row = genres.scores()["families"]["country"]
    assert row["producer"] == 9.4
    assert row["producer_n"] == 2
    assert row["briefed"] == 1, "the two counts are not the same count"


def test_a_liked_family_beats_a_disliked_one_with_fewer_samples():
    """The exploration term must break ties, not overrule the ratings.

    UCB1's c=1 assumes rewards spanning [0,1]. A single rater on a ten-point
    scale spans about four of those points, so at c=1 the uncertainty term at
    the ranking threshold (1.194) exceeds the entire reward interval and the
    allocator prefers whatever it has seen least, forever. This shipped that
    way: a family rated 4.5 over 8 briefs outscored one rated 8.0 over 30, and
    it read as a preference rather than a bug because the printed sum was
    perfectly self-consistent.
    """
    liked = genres._scaled(8.0) + genres._bonus(30, 300)
    disliked = genres._scaled(4.5) + genres._bonus(8, 300)
    assert liked > disliked

    # The premium for being under-sampled is worth about one rating point: the
    # smallest difference one rater can honestly express. Much more and taste
    # stops deciding; much less and nothing new is ever tried.
    premium = (genres._bonus(8, 300) - genres._bonus(30, 300)) * 9
    assert 0.5 < premium < 2.0, f"{premium:.2f} rating points"


# ── the rhythm floor ─────────────────────────────────────────────────────────
def test_the_day_always_contains_rhythm_led_music():
    """The one rule in the module that is a taste decision rather than a
    measurement. The studio ships to vertical video and a day of piano ballads
    cannot be used there no matter how well it scores — on 2026-08-30 coverage
    rotation gave three of five released tracks at or below 96 BPM, two of them
    on piano and fingerpicked guitar, and the allocator was working correctly."""
    rows = genres.slate(7, rhythm_floor=2, explore_briefs=0)
    got = sum(r["specs"] for r in rows if r["genre_family"] in genres.RHYTHM_LED)
    assert got >= 2
    assert sum(r["specs"] for r in rows) == 7


def test_the_floor_survives_a_warm_record_that_favours_folk():
    """A floor that a good month for folk can talk out of is a preference."""
    for offset, (family, rating) in enumerate(
            (("folk", 9), ("country", 9), ("rock", 8))):
        _stock(family, n=12, rating=rating, start=200 + offset * 40)
    rows = genres.slate(7, rhythm_floor=2, explore_briefs=0)
    got = sum(r["specs"] for r in rows if r["genre_family"] in genres.RHYTHM_LED)
    assert got >= 2, [(r["genre_family"], r["specs"], r["stance"]) for r in rows]


def test_the_floor_never_takes_more_than_half_the_slate():
    """A floor that can take the whole day is not a floor, it is the allocator.
    slate(2) with a floor of 2 would leave nothing for evidence or an external
    chart to decide."""
    rows = genres.slate(2, rhythm_floor=2, explore_briefs=0)
    got = sum(r["specs"] for r in rows if r["genre_family"] in genres.RHYTHM_LED)
    assert got <= 1
    assert sum(r["specs"] for r in rows) == 2

    single = genres.slate(1, rhythm_floor=2, explore_briefs=0)
    assert sum(r["specs"] for r in single) == 1


def test_an_exploration_pick_that_lands_on_rhythm_counts_toward_the_floor():
    """Otherwise the two floors spend the same brief twice and a seven-brief day
    has four of its slots decided before the allocator sees them."""
    rows = genres.slate(7, rhythm_floor=2, explore_briefs=2)
    assert sum(r["specs"] for r in rows) == 7
    floored = sum(r["specs"] for r in rows if r["stance"] == "floor")
    assert floored <= 2


def test_the_floor_picks_are_labelled_as_a_decision_not_as_evidence():
    """It biases the record — a family inside RHYTHM_LED accumulates rated
    briefs faster than one outside it — so the console has to be able to say
    which picks were taste rather than measurement."""
    rows = genres.slate(7, rhythm_floor=2, explore_briefs=0)
    floor_rows = [r for r in rows if r["stance"] == "floor"]
    assert floor_rows
    for r in floor_rows:
        assert r["genre_family"] in genres.RHYTHM_LED
        assert "rhythm floor" in r["why"]


def test_the_fusions_are_reachable_and_belong_to_real_families():
    """The user-facing point of the whole change: a crossbreed is now a label
    the Director can copy verbatim, not something it has to invent in prose."""
    for fusion in ("electronic-hip-hop", "techno-r-and-b", "electro-r-and-b",
                   "jersey-club", "phonk", "hyperpop", "latin-house",
                   "melodic-techno", "drum-and-bass"):
        assert fusion in SPECIFICS, fusion
        assert SPECIFICS[fusion] in FAMILIES
        assert fusion in VOCABULARY[SPECIFICS[fusion]]


def test_every_rhythm_led_family_is_a_real_family():
    assert genres.RHYTHM_LED <= set(FAMILIES)
