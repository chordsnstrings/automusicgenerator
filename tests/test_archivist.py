"""The loop only means something if it reports which signal it is running on."""

from datetime import date

from dailyfive.archivist import (MIN_OBSERVATIONS, _bpm_bucket, _clip_value,
                                 _first_reason, _style_tokens, aggregate,
                                 apply_learning, learning_status, rate)
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
