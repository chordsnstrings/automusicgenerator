"""A&R is the only role looking at the day as a set, so it is where genre spread
has to be visible.

``docs/ARCHITECTURE.md`` has claimed since it was written that A&R "enforces
spread across genre, tempo and mood". Until the genre column existed there was
no genre to spread — on the studio's first real day three of six briefs were the
same family and nothing in the pipeline noticed.
"""

from __future__ import annotations

import pytest

from dailyfive.agents import anr


@pytest.fixture
def codex():
    from dailyfive.codex import current
    return current()


@pytest.fixture
def answers(monkeypatch):
    box = {"briefs": [], "user": ""}

    def fake_ask_json(role, system, user, *, schema_hint=None, **kw):
        box["user"] = user
        return {"briefs": box["briefs"]}

    monkeypatch.setattr(anr, "ask_json", fake_ask_json)
    return box


def _spec(i, family, **kw):
    return {"theme": f"t{i}", "slot_type": "full", "bpm": 84,
            "style_string": f"variant {i}", "genre_family": family,
            "genre": kw.get("genre", "country-soul")}


def test_the_genre_is_carried_from_the_spec_not_restated_by_the_model(answers, codex):
    """A model asked to restate a decision it did not make will occasionally
    restate it differently, and a genre that drifts between the spec and the
    brief is two labels for one song."""
    answers["briefs"] = [{"title": "A", "diversity_vector":
                          {"mood": "numb", "genre_family": "electronic"}}]
    briefs = anr.run([_spec(0, "country")], codex)
    assert briefs[0]["diversity_vector"]["genre_family"] == "country"


def test_the_family_reaches_the_duplicate_key(answers, codex):
    answers["briefs"] = [{"title": "A", "diversity_vector": {"mood": "numb",
                                                             "tempo_band": "ballad",
                                                             "subject": "leaving"}},
                         {"title": "B", "diversity_vector": {"mood": "numb",
                                                             "tempo_band": "ballad",
                                                             "subject": "leaving"}}]
    same = anr.run([_spec(0, "country"), _spec(1, "country")], codex)
    assert anr._flag_duplicates(same) == [("A", "B")]

    # A ballad about leaving is a different record in country and in
    # electronic, so the same three words no longer make them one song.
    split = anr.run([_spec(0, "country"), _spec(1, "electronic")], codex)
    assert anr._flag_duplicates(split) == []


def test_a_brief_with_no_genre_still_has_its_other_dimensions_compared(answers, codex):
    """The six briefs already in production predate genre being recorded and
    carry a NULL. The duplicate check has to keep working over them."""
    vector = {"mood": "numb", "tempo_band": "ballad", "subject": "leaving"}
    answers["briefs"] = [{"title": "A", "diversity_vector": dict(vector)},
                         {"title": "B", "diversity_vector": dict(vector)}]
    briefs = anr.run([_spec(0, None), _spec(1, None)], codex)
    assert all(b["diversity_vector"]["genre_family"] is None for b in briefs)
    assert anr._flag_duplicates(briefs) == [("A", "B")]


def test_the_model_is_told_the_genre_was_decided_before_it(answers, codex):
    answers["briefs"] = [{"title": "A"}]
    anr.run([_spec(0, "country")], codex)
    assert "genre_family" in answers["user"], "the spec's genre never reached the prompt"
    assert "not yours to change" in anr.SYSTEM
