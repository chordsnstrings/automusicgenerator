"""The loop only means something if it reports which signal it is running on."""

import json
from datetime import date

from dailyfive.archivist import (MIN_OBSERVATIONS, _bpm_bucket, _clip_value,
                                 _first_reason, _style_tokens, aggregate,
                                 apply_learning, learning_status, rate,
                                 unrate)
from dailyfive.codex import current as current_codex
from dailyfive.db import session_scope
from dailyfive.models import Clip, Outcome, SlotType


def _job(run_id, brief_id):
    from dailyfive.models import Job, JobState
    with session_scope() as s:
        j = Job(run_id=run_id, brief_id=brief_id,
                idempotency_key=f"k{run_id}:{brief_id}", state=JobState.SUCCESS,
                payload={})
        s.add(j)
        s.flush()
        return j.id


def _clip(run_id, brief_id, *, style="dark rnb, 808s", score=8.0, bpm=84, n=1):
    ids = []
    job_id = _job(run_id, brief_id)
    with session_scope() as s:
        for i in range(n):
            c = Clip(run_id=run_id, job_id=job_id, brief_id=brief_id,
                     audio_id=f"a{brief_id}-{i}-{style[:4]}", variant=i,
                     slot_type=SlotType.FULL, style_string=style,
                     bpm_target=bpm, score_total=score, qc_verdict="pass")
            s.add(c)
            s.flush()
            ids.append(c.id)
    return ids


def test_fresh_install_says_it_has_no_human_signal():
    assert "producer-only" in learning_status()["signal"]


def test_rating_changes_the_reported_signal(run_id, brief_factory):
    bid = brief_factory()
    ids = _clip(run_id, bid, n=2)
    with session_scope() as s:
        for i in ids:
            s.get(Clip, i).shipped = True
    rate(ids[0], 9)
    rate(ids[1], 7)
    st = learning_status()
    assert st["rated"] == 2
    assert "rating-led" in st["signal"]


def test_rating_is_clamped_to_the_scale(run_id, brief_factory):
    bid = brief_factory()
    cid = _clip(run_id, bid)[0]
    rate(cid, 99)
    with session_scope() as s:
        assert s.query(Outcome).filter(Outcome.clip_id == cid).one().rating == 10


def test_rating_twice_updates_rather_than_duplicating(run_id, brief_factory):
    bid = brief_factory()
    cid = _clip(run_id, bid)[0]
    rate(cid, 3)
    rate(cid, 8)
    with session_scope() as s:
        rows = s.query(Outcome).filter(Outcome.clip_id == cid).all()
        assert len(rows) == 1 and rows[0].rating == 8


def test_a_human_rating_outweighs_the_producer_score():
    from dailyfive.models import Clip as C
    clip = C(score_total=9.0)
    assert _clip_value(clip, None) < 9.0          # damped toward the midpoint
    assert _clip_value(clip, Outcome(rating=2)) == 2.0


def test_thin_evidence_is_not_reported_as_a_trend(run_id, brief_factory):
    bid = brief_factory()
    _clip(run_id, bid, style="rare style", n=MIN_OBSERVATIONS - 1)
    assert "rare style" not in aggregate()["style_scores"]


def test_enough_observations_produce_an_aggregate(run_id, brief_factory):
    bid = brief_factory()
    _clip(run_id, bid, style="common style", n=MIN_OBSERVATIONS)
    assert "common style" in aggregate()["style_scores"]


def test_codex_is_not_edited_before_there_is_evidence():
    assert apply_learning()["changed"] is False


def test_helpers():
    assert _bpm_bucket(70) == "ballad" and _bpm_bucket(140) == "club"
    assert _style_tokens("dark rnb, 808s, x") == ["dark rnb", "808s"]
    assert _first_reason("true peak +0.4 dBFS is clipping; 900 samples") == "clipping"


def test_unrate_clears_the_rating_and_when_it_was_given(run_id, brief_factory):
    bid = brief_factory()
    cid = _clip(run_id, bid)[0]
    rate(cid, 7)
    assert unrate(cid) is True
    with session_scope() as s:
        row = s.query(Outcome).filter(Outcome.clip_id == cid).one()
        assert row.rating is None
        assert row.rated_at is None


def test_unrate_keeps_the_note_that_came_with_the_rating(run_id, brief_factory):
    """The assertion that stops this being simplified into a row delete.

    Every reader filters on rating IS NOT NULL, so null-ing and deleting look
    identical from the outside — which is exactly why the difference has to be
    pinned here. The mistake being undone is a mis-tap on a 1-10 widget; the
    prose you typed is not part of it.
    """
    bid = brief_factory()
    cid = _clip(run_id, bid)[0]
    rate(cid, 7, note="the bridge does not land")
    with session_scope() as s:
        s.query(Outcome).filter(Outcome.clip_id == cid).one().plays = 4

    unrate(cid)

    with session_scope() as s:
        row = s.query(Outcome).filter(Outcome.clip_id == cid).one()
        assert row.note == "the bridge does not land"
        assert row.plays == 4


def test_unrate_is_idempotent_and_never_raises(run_id, brief_factory):
    """A double-tap on a clear control must not error."""
    bid = brief_factory()
    cid = _clip(run_id, bid)[0]
    assert unrate(cid) is False, "never rated"
    rate(cid, 5)
    assert unrate(cid) is True
    assert unrate(cid) is False
    assert unrate(4242424) is False, "a clip with no outcome row at all"


def test_clearing_a_rating_moves_the_learning_signal_back(run_id, brief_factory):
    """The point of the fix: a wrong value steers the loop until it is removed.

    _clip_value falls back to the Producer's damped score once the rating is
    gone, so the number the weekly retro reads changes — which is the whole
    reason a bogus rating had to be removable.
    """
    bid = brief_factory()
    cid = _clip(run_id, bid, score=9.0)[0]
    with session_scope() as s:
        s.get(Clip, cid).shipped = True
    rate(cid, 1)
    assert learning_status()["rated"] == 1
    with session_scope() as s:
        clip, outcome = s.get(Clip, cid), s.query(Outcome).one()
        assert _clip_value(clip, outcome) == 1.0

    unrate(cid)

    assert learning_status()["rated"] == 0
    assert "producer-only" in learning_status()["signal"]
    with session_scope() as s:
        clip = s.get(Clip, cid)
        assert _clip_value(clip, s.query(Outcome).one()) > 5.0


# ── genre reaches the codex, or is refused entry ─────────────────────────────
def _rated_family(family, specific, ratings, *, start=500):
    """One rated brief per day in one family, the way the pipeline produces them.

    A day at a time on purpose: two briefs of the same family on one date are
    two rows but one day's noise, and the whole point of the threshold is that
    such a day cannot promote anything on its own.
    """
    from datetime import timedelta

    from dailyfive.models import (Brief, Job, JobState, Outcome, Run, RunPhase,
                                  SlotType, utcnow)
    for i, score in enumerate(ratings):
        with session_scope() as s:
            r = Run(run_date=date(2026, 1, 1) + timedelta(days=start + i),
                    phase=RunPhase.SHIPPED)
            s.add(r); s.flush(); rid = r.id
        with session_scope() as s:
            b = Brief(run_id=rid, slot_type=SlotType.FULL, idx=0, title=f"t{i}",
                      theme="a specific situation", genre_family=family,
                      genre=specific, style_string="close-mic vocal")
            s.add(b); s.flush()
            j = Job(run_id=rid, brief_id=b.id, idempotency_key=f"g{rid}",
                    state=JobState.SUCCESS, payload={})
            s.add(j); s.flush()
            first = None
            for v in (0, 1):
                c = Clip(run_id=rid, job_id=j.id, brief_id=b.id,
                         audio_id=f"g{rid}-{v}", variant=v, slot_type=SlotType.FULL,
                         genre_family=family, genre=specific, shipped=v == 0,
                         style_string=b.style_string, qc_verdict="pass",
                         score_total=6.0)
                s.add(c); s.flush()
                first = first or c.id
            s.add(Outcome(clip_id=first, rating=score, rated_at=utcnow()))


def test_apply_learning_refuses_to_write_a_thin_genre_score():
    """Seven rated briefs is not eight, and the codex says nothing rather than
    handing the Director a number it is told beats its priors."""
    from dailyfive.genres import GENRE_MIN_RATED
    _rated_family("country", "country-soul", [9] * (GENRE_MIN_RATED - 1))
    apply_learning()
    learned = current_codex().body["learned"]
    assert learned["genre_scores"] == {}
    assert learned["subgenre_scores"] == {}


def test_the_last_rated_brief_before_the_bar_is_what_puts_a_genre_in_the_codex():
    from dailyfive.genres import GENRE_MIN_RATED
    _rated_family("country", "country-soul", [9] * GENRE_MIN_RATED)
    apply_learning()
    learned = current_codex().body["learned"]
    assert learned["genre_scores"]["country"]["n"] == GENRE_MIN_RATED
    assert learned["genre_scores"]["country"]["mean"] > 8.5
    assert learned["subgenre_scores"]["country-soul"]["n"] == GENRE_MIN_RATED


def test_a_genre_score_never_reaches_the_director_without_its_sample_count():
    """director.py tells the model a learned observation beats its priors. A
    mean arriving without its n is a mean the model cannot discount, which is
    exactly how "no synths 6.11" came to look like a finding."""
    from dailyfive.genres import GENRE_MIN_RATED
    _rated_family("folk", "indie-folk", [7] * GENRE_MIN_RATED)
    apply_learning()
    context = current_codex().brief_context()
    assert "folk" in context
    assert f"over {GENRE_MIN_RATED} rated briefs" in context
    for line in context.splitlines():
        if line.startswith("genre observed"):
            assert "rated brief" in line
            break
    else:
        raise AssertionError("no genre line in the Director's context")


def test_the_retros_vocabulary_proposal_never_reaches_the_directors_prompt():
    """It is a proposal for a person to make as a code change. Rendered into
    the prompt as a note it would be an instruction to write a word the
    vocabulary does not carry — normalise() would null it, and the brief would
    lose the genre the note was arguing for."""
    from dailyfive.codex import SEED_CODEX, save_new_version
    body = json.loads(json.dumps(SEED_CODEX))
    body["learned"]["genre_vocabulary_note"] = "add afrobeats; three off-vocabulary specs"
    save_new_version(body, [], diff="test", rationale="test")
    context = current_codex().brief_context()
    assert "afrobeats" not in context
    assert current_codex().body["learned"]["genre_vocabulary_note"]
