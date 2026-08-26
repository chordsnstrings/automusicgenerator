"""The ledger is what makes the roster checkable rather than notional."""

from dailyfive import ledger
from dailyfive.llm import Brain


def test_calls_are_attributed_to_the_bound_run(run_id):
    ledger.bind_run(run_id)
    ledger.record_call("scout", Brain("minimax", "MiniMax-M3"), "sense",
                       ok=True, ms=1200, chars_in=9000, chars_out=2400)
    ledger.bind_run(None)
    rows = ledger.for_run(run_id)
    assert len(rows) == 1
    assert rows[0]["provider"] == "minimax" and rows[0]["role"] == "scout"


def test_which_brain_answered_is_recorded(run_id):
    """Output quality changing is only traceable if the brain is on the row."""
    ledger.bind_run(run_id)
    ledger.record_call("lyricist", Brain("anthropic", "claude-opus-5"), "draft1",
                       ok=True, ms=800, chars_in=500, chars_out=1200)
    ledger.bind_run(None)
    row = ledger.for_run(run_id)[0]
    assert (row["provider"], row["model"]) == ("anthropic", "claude-opus-5")


def test_failures_are_recorded_not_dropped(run_id):
    ledger.bind_run(run_id)
    ledger.record_call("producer", None, "score", ok=False, ms=300,
                       chars_in=900, chars_out=0, error="rate limited")
    ledger.bind_run(None)
    row = ledger.for_run(run_id)[0]
    assert row["ok"] is False and "rate limited" in row["error"]
    assert ledger.role_summary()["producer"]["failures"] == 1


def test_summary_totals_across_calls(run_id):
    ledger.bind_run(run_id)
    for _ in range(3):
        ledger.record_call("scout", Brain("minimax", "MiniMax-M3"), "sense",
                           ok=True, ms=1000, chars_in=100, chars_out=200)
    ledger.bind_run(None)
    st = ledger.role_summary()["scout"]
    assert st["calls"] == 3 and st["ms"] == 3000 and st["chars_out"] == 600


def test_a_ledger_failure_never_takes_down_a_run(monkeypatch):
    """Losing an audit row is a nuisance; losing the day's songs is not."""
    def broken():
        raise RuntimeError("database on fire")
    monkeypatch.setattr(ledger, "session_scope", broken)
    ledger.record_call("scout", None, "x", ok=True, ms=1, chars_in=1, chars_out=1)


def test_unbound_calls_are_still_recorded():
    """A retro or a probe runs outside a run and must still show up."""
    ledger.bind_run(None)
    ledger.record_call("retro", Brain("minimax", "MiniMax-M3"), "weekly",
                       ok=True, ms=500, chars_in=10, chars_out=20)
    assert ledger.recent(5)[0]["run_id"] is None
