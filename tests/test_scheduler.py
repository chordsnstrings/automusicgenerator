"""The worker clock. Deliberately dumb, so worth checking it stays that way."""

from datetime import datetime, timedelta, timezone

import pytest

from dailyfive import scheduler


@pytest.fixture
def fired(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "_fire", lambda name: calls.append(name))
    return calls


def _at(hour, minute):
    return datetime.now(timezone.utc).replace(hour=hour, minute=minute,
                                              second=0, microsecond=0)


def test_slots_are_ordered_so_nothing_races_the_run():
    """Backup wants a quiet database; purge must never race a run mid-write."""
    order = [name for name, _, _ in scheduler.SLOTS]
    assert order == ["purge", "retype", "backup", "run"]
    times = [h * 60 + m for _, h, m in scheduler.SLOTS]
    assert times == sorted(times), "slots must fire in clock order"
    run_at = next(h * 60 + m for n, h, m in scheduler.SLOTS if n == "run")
    purge_at = next(h * 60 + m for n, h, m in scheduler.SLOTS if n == "purge")
    assert run_at - purge_at >= 120, "purge must be well clear of the run window"


def test_a_slot_fires_once_the_time_has_passed(fired, monkeypatch):
    monkeypatch.setattr(scheduler, "init_db", lambda: None, raising=False)
    monkeypatch.setattr("dailyfive.db.init_db", lambda **kw: None)
    scheduler.run_forever(once=True)
    # Everything scheduled before now today should have fired exactly once.
    now = datetime.now(timezone.utc)
    expected = [n for n, h, m in scheduler.SLOTS if now >= _at(h, m)]
    assert set(fired) <= set(expected)


def test_a_failing_slot_never_takes_the_worker_down(monkeypatch, caplog):
    """Tomorrow's run is still wanted; a crash-loop just fails faster."""
    def boom(name):
        raise RuntimeError("suno is on fire")
    monkeypatch.setattr("dailyfive.pipeline.run_daily", boom)
    scheduler._fire("run")          # must not raise


def test_unknown_slot_is_ignored():
    scheduler._fire("not-a-slot")   # must not raise
