"""The Conductor is the reliability core, tested against a scripted fake Suno."""

from datetime import timedelta

import pytest

from dailyfive.conductor import Conductor, _rank
from dailyfive.db import session_scope
from dailyfive.errors import ProviderError
from dailyfive.models import Clip, Job, JobState, utcnow


class FakeSuno:
    """Scriptable stand-in. Counts calls so double-spends are visible."""

    def __init__(self, script=None, credits_value=1000):
        self.script = script or {}
        self.generate_calls = 0
        self.record_calls = 0
        self.credits_value = credits_value

    def generate(self, payload):
        self.generate_calls += 1
        return f"task-{self.generate_calls}"

    def credits(self):
        return self.credits_value

    def record_info(self, task_id):
        self.record_calls += 1
        seq = self.script.get(task_id, [])
        idx = min(self.record_calls - 1, len(seq) - 1) if seq else 0
        return seq[idx] if seq else {"status": "PENDING"}


def _queue(run_id, brief_id, payload=None):
    with session_scope() as s:
        j = Job(run_id=run_id, brief_id=brief_id,
                idempotency_key=f"{run_id}:{brief_id}:h",
                payload=payload or {"model": "V5_5"}, state=JobState.QUEUED)
        s.add(j)
        s.flush()
        return j.id


def _success(n=2):
    return {"status": "SUCCESS", "response": {"sunoData": [
        {"id": f"audio-{i}", "audio_url": f"https://x/{i}.mp3", "title": f"T{i}",
         "duration": 190.0 + i} for i in range(n)]}}


def test_submit_records_task_id(run_id, brief_factory):
    bid = brief_factory()
    job_id = _queue(run_id, bid)
    fake = FakeSuno()
    assert Conductor(run_id, client=fake).submit_all() == [job_id]
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.task_id == "task-1"
        assert job.state == JobState.SUBMITTED


def test_resubmit_never_pays_twice(run_id, brief_factory):
    """The whole point of the idempotency key: a restart resumes, never re-buys."""
    bid = brief_factory()
    job_id = _queue(run_id, bid)
    fake = FakeSuno()
    c = Conductor(run_id, client=fake)
    c.submit_all()
    assert fake.generate_calls == 1
    c.submit_all()          # simulate a crashed process being restarted
    assert fake.generate_calls == 1
    with session_scope() as s:
        assert s.get(Job, job_id).task_id == "task-1"


def test_success_records_both_clips(run_id, brief_factory):
    bid = brief_factory()
    job_id = _queue(run_id, bid)
    c = Conductor(run_id, client=FakeSuno())
    c.submit_all()
    c.ingest_record(job_id, _success(), source="callback")
    with session_scope() as s:
        clips = s.query(Clip).filter(Clip.job_id == job_id).all()
        assert len(clips) == 2
        assert {cl.variant for cl in clips} == {0, 1}


def test_duplicate_delivery_does_not_duplicate_clips(run_id, brief_factory):
    """FIRST_SUCCESS then SUCCESS delivers clip one twice. That must be harmless."""
    bid = brief_factory()
    job_id = _queue(run_id, bid)
    c = Conductor(run_id, client=FakeSuno())
    c.submit_all()
    c.ingest_record(job_id, {"status": "FIRST_SUCCESS",
                             "response": {"sunoData": _success(1)["response"]["sunoData"]}})
    c.ingest_record(job_id, _success(2))
    with session_scope() as s:
        assert s.query(Clip).filter(Clip.job_id == job_id).count() == 2


def test_moderation_failure_is_terminal(run_id, brief_factory):
    bid = brief_factory()
    job_id = _queue(run_id, bid)
    c = Conductor(run_id, client=FakeSuno())
    c.submit_all()
    state = c.ingest_record(job_id, {"status": "SENSITIVE_WORD_ERROR"})
    assert state == JobState.FAILED
    with session_scope() as s:
        assert s.get(Job, job_id).failure_kind == "moderation"


def test_state_never_goes_backwards(run_id, brief_factory):
    """A late PENDING must not undo a SUCCESS."""
    bid = brief_factory()
    job_id = _queue(run_id, bid)
    c = Conductor(run_id, client=FakeSuno())
    c.submit_all()
    c.ingest_record(job_id, _success())
    c.ingest_record(job_id, {"status": "PENDING"})
    with session_scope() as s:
        assert s.get(Job, job_id).state == JobState.SUCCESS


def test_unknown_status_is_logged_not_swallowed(run_id, brief_factory):
    bid = brief_factory()
    job_id = _queue(run_id, bid)
    c = Conductor(run_id, client=FakeSuno())
    c.submit_all()
    c.ingest_record(job_id, {"status": "SOMETHING_NEW"})
    with session_scope() as s:
        assert "SOMETHING_NEW" in (s.get(Job, job_id).last_error or "")


def test_polling_completes_a_run_with_no_callbacks_at_all(run_id, brief_factory):
    """The failure this design exists to survive: every webhook is lost."""
    bid = brief_factory()
    job_id = _queue(run_id, bid)
    fake = FakeSuno(script={"task-1": [{"status": "PENDING"}, _success()]})
    c = Conductor(run_id, client=fake)
    c.submit_all()
    with session_scope() as s:
        s.get(Job, job_id).last_seen_at = utcnow() - timedelta(minutes=5)
    c.await_all(timeout_s=5, poll_interval_s=0, sleep=lambda _s: None)
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.state in (JobState.SUCCESS, JobState.MIRRORED)
        assert job.callbacks_seen == 0
        assert job.polls > 0


def test_jobs_that_never_finish_are_abandoned_not_hung(run_id, brief_factory):
    bid = brief_factory()
    job_id = _queue(run_id, bid)
    fake = FakeSuno(script={"task-1": [{"status": "PENDING"}]})
    c = Conductor(run_id, client=fake)
    c.submit_all()
    with session_scope() as s:
        s.get(Job, job_id).last_seen_at = utcnow() - timedelta(minutes=5)
    c.await_all(timeout_s=0.1, poll_interval_s=0, sleep=lambda _s: None)
    with session_scope() as s:
        assert s.get(Job, job_id).state == JobState.ABANDONED


def test_non_retryable_submit_failure_is_terminal(run_id, brief_factory):
    class Broken(FakeSuno):
        def generate(self, payload):
            raise ProviderError("suno", "bad params", retryable=False)

    bid = brief_factory()
    job_id = _queue(run_id, bid)
    Conductor(run_id, client=Broken()).submit_all()
    with session_scope() as s:
        assert s.get(Job, job_id).state == JobState.FAILED


def test_rank_orders_progress():
    assert _rank(JobState.SUCCESS) > _rank(JobState.PENDING)
    assert _rank(JobState.MIRRORED) > _rank(JobState.SUCCESS)
    assert _rank(JobState.FAILED) == 99
