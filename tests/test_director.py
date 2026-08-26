"""The prompt describes the lanes the run has, not the lanes the studio knows."""

import pytest

from dailyfive.agents import director


@pytest.fixture
def codex():
    from dailyfive.codex import current
    return current()


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def fake_ask_json(role, system, user, *, schema_hint=None, **kw):
        seen.update(system=system, user=user, schema=schema_hint)
        return {"specs": [{"theme": "t", "slot_type": "full", "bpm": 84,
                           "style_string": "dark rnb"}]}

    monkeypatch.setattr(director, "ask_json", fake_ask_json)
    return seen


def test_no_short_lane_means_no_short_instruction(captured, codex):
    director.run([{"theme": "t"}] * 10, codex, full_n=7, short_n=0)
    blob = captured["system"] + captured["user"] + captured["schema"]
    assert "SHORT" not in blob and "short" not in blob
    assert "Write 7 FULL specs." in captured["user"]


def test_the_short_lane_is_briefed_when_it_has_slots(captured, codex):
    director.run([{"theme": "t"}] * 10, codex, full_n=4, short_n=3)
    assert "Write 4 FULL specs and 3 SHORT specs — 7 in total." in captured["user"]
    assert "30-60 second cuts built to loop" in captured["system"]
    assert '"slot_type": "full|short"' in captured["schema"]
