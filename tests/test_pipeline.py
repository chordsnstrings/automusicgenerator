"""Phase ordering and resume: a crashed run must not re-spend."""

from datetime import date

from dailyfive import pipeline as pl
from dailyfive.db import session_scope
from dailyfive.models import (Brief, Clip, Job, JobState, Run, RunPhase, Signal,
                              SlotType)


def test_phase_ordering():
    assert pl._before(RunPhase.CREATED, RunPhase.SENSED)
    assert not pl._before(RunPhase.SHIPPED, RunPhase.SENSED)
    assert pl._before(RunPhase.FAILED, RunPhase.SENSED), \
        "a failed run must re-enter the phase chain"


def test_preflight_reports_every_problem_at_once():
    problems = pl.preflight(require_ffmpeg=True)
    assert len(problems) >= 2
    joined = " ".join(problems)
    assert "SUNO_API_KEY" in joined
    assert "personas" in joined


def test_opening_the_same_date_twice_resumes_rather_than_duplicating():
    a, _ = pl._open_run(date(2026, 8, 27), resume=True)
    b, _ = pl._open_run(date(2026, 8, 27), resume=True)
    assert a == b
    with session_scope() as s:
        assert s.query(Run).count() == 1


def test_no_resume_refuses_an_existing_run():
    pl._open_run(date(2026, 8, 27), resume=True)
    try:
        pl._open_run(date(2026, 8, 27), resume=False)
    except RuntimeError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected a refusal")


def test_failed_run_resumes_from_what_the_database_actually_shows(run_id,
                                                                 brief_factory):
    """Phase is inferred from rows, not trusted from the column."""
    bid = brief_factory()
    with session_scope() as s:
        s.add(Job(run_id=run_id, brief_id=bid, idempotency_key="k",
                  task_id="task-1", state=JobState.SUCCESS, payload={}))
        s.get(Run, run_id).phase = RunPhase.FAILED
    _, phase = pl._open_run(date(2026, 8, 27), resume=True)
    assert phase == RunPhase.SUBMITTED


def test_inferred_phase_walks_back_to_created_on_an_empty_run(run_id):
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.CREATED


def test_inferred_phase_recognises_each_stage(run_id, brief_factory):
    with session_scope() as s:
        s.add(Signal(run_id=run_id, rank=1, theme="t", sentiment="s"))
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.SENSED

    bid = brief_factory()
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.WRITTEN  # fixture sets lyrics

    with session_scope() as s:
        j = Job(run_id=run_id, brief_id=bid, idempotency_key="k2",
                task_id="t1", state=JobState.SUCCESS, payload={})
        s.add(j)
        s.flush()
        job_id = j.id
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.SUBMITTED

    with session_scope() as s:
        s.add(Clip(run_id=run_id, job_id=job_id, brief_id=bid, audio_id="a1",
                   slot_type=SlotType.FULL, local_path="/tmp/x.mp3"))
    with session_scope() as s:
        assert pl._infer_phase(s, run_id) == RunPhase.RENDERED


def test_brief_dict_round_trips_the_fields_agents_need(run_id, brief_factory):
    bid = brief_factory(title="Slow Burn", bpm=84)
    with session_scope() as s:
        d = pl._brief_dict(s.get(Brief, bid))
    for key in ("title", "theme", "slot_type", "bpm", "style_string", "lyrics"):
        assert key in d
    assert d["slot_type"] == "full" and d["bpm"] == 84
