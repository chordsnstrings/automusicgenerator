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


# ── genre: a controlled term the Director copies, not prose it writes ────────
SLATE = [{"genre_family": "country", "specs": 4, "stance": "exploit", "mean": 7.4,
          "n": 12, "basis": "your ratings", "why": "highest sum"},
         {"genre_family": "folk", "specs": 3, "stance": "explore", "mean": None,
          "n": 0, "basis": "never briefed", "why": "coverage"}]


@pytest.fixture
def spec_capture(monkeypatch):
    """Lets a test put arbitrary specs on the wire and read back what survived."""
    box = {"specs": [], "seen": {}}

    def fake_ask_json(role, system, user, *, schema_hint=None, **kw):
        box["seen"].update(system=system, user=user, schema=schema_hint)
        return {"specs": box["specs"]}

    monkeypatch.setattr(director, "ask_json", fake_ask_json)
    return box


def _spec(**kw):
    base = {"theme": "t", "slot_type": "full", "bpm": 84, "key": "F minor",
            "song_form": "Verse - Chorus", "style_string": "brushed drums, upright bass"}
    return {**base, **kw}


def test_the_slate_arrives_as_counts_with_the_vocabulary_beside_it(captured, codex):
    director.run([{"theme": "t"}] * 10, codex, full_n=7, short_n=0, slate=SLATE)
    assert "Today's genre slate — 7 specs:" in captured["user"]
    assert '"country-soul"' in captured["user"], "the vocabulary was not supplied"
    assert "COUNTS, not an assignment" in captured["system"]


def test_with_no_slate_the_absence_is_stated_and_the_fields_are_still_asked_for(captured, codex):
    """An absent section would leave the model to infer why it is being asked
    for a genre with no evidence attached."""
    director.run([{"theme": "t"}] * 10, codex, full_n=7, short_n=0)
    assert "No genre slate today" in captured["user"]
    assert "COUNTS, not an assignment" not in captured["system"]
    assert '"genre_family"' in captured["schema"]


def test_a_loosely_spelled_genre_is_normalised_not_rejected(spec_capture, codex):
    spec_capture["specs"] = [_spec(genre_family="Country", genre="Country Soul")]
    out = director.run([{"theme": "t"}], codex, full_n=1, short_n=0, slate=SLATE)
    assert (out[0]["genre_family"], out[0]["genre"]) == ("country", "country-soul")


def test_an_off_vocabulary_genre_is_kept_with_nulls(spec_capture, codex):
    """Dropping the spec would cost a whole generation over a label, and the
    count of off-vocabulary answers is the evidence a term is missing."""
    spec_capture["specs"] = [_spec(genre_family="jazz", genre="bebop")]
    out = director.run([{"theme": "t"}], codex, full_n=1, short_n=0, slate=SLATE)
    assert len(out) == 1
    assert out[0]["genre_family"] is None and out[0]["genre"] is None
    assert out[0]["style_string"] == "brushed drums, upright bass"


def test_a_spec_that_answers_with_the_genre_word_is_flagged_and_still_kept(spec_capture, codex):
    """Handing a model the word "country" invites it to write "country" back.
    The prompt asks it not to; this notices when it did anyway."""
    spec_capture["specs"] = [_spec(genre_family="country", genre="country-soul",
                                   style_string="country, country-soul")]
    out = director.run([{"theme": "t"}], codex, full_n=1, short_n=0, slate=SLATE)
    assert out[0]["genre_label_only"] is True
    assert out[0]["style_string"] == "country, country-soul", "the spec was rewritten"


def test_a_real_specification_carrying_a_genre_is_not_flagged(spec_capture, codex):
    spec_capture["specs"] = [_spec(genre_family="country", genre="country-soul")]
    out = director.run([{"theme": "t"}], codex, full_n=1, short_n=0, slate=SLATE)
    assert "genre_label_only" not in out[0]


def test_a_genre_cannot_crowd_out_the_tempo_and_the_form(spec_capture, codex):
    """The other half of "the genre field buys you nothing on the rest of the
    spec": a labelled spec that arrives with neither a tempo nor a form has had
    its checkable core replaced by the label."""
    spec_capture["specs"] = [_spec(genre_family="country", genre="country-soul",
                                   bpm=None, song_form=None,
                                   style_string="warm room mic, brushed drums")]
    out = director.run([{"theme": "t"}], codex, full_n=1, short_n=0, slate=SLATE)
    assert out[0]["genre_label_only"] is True


def test_the_word_the_director_chose_survives_into_the_ledger(spec_capture, codex):
    """The count of off-vocabulary answers is the evidence a term is missing;
    the WORD is the evidence of which term. run() normalises in its own
    cleaning loop and writes the null before genres.enforce() ever sees the
    spec, so enforce() reading the two live fields recorded an empty string
    every time and "afrobeats" never reached the ledger or the console."""
    from dailyfive import genres

    spec_capture["specs"] = [_spec(genre_family="afrobeats", genre="amapiano")]
    out = director.run([{"theme": "t"}], codex, full_n=1, short_n=0, slate=SLATE)
    assert out[0]["genre_family"] is None
    assert out[0]["genre_off_vocabulary"] == "afrobeats"

    ledger = genres.enforce(out, SLATE)
    assert ledger["unlabelled"] == 1
    assert ledger["off_vocabulary"] == ["afrobeats"]


def test_a_spec_that_named_no_genre_is_counted_but_contributes_no_word(spec_capture, codex):
    """Not the same evidence. One says the vocabulary is missing a term; the
    other says the Director skipped the field, and a blank string in the list
    the console reads out as words would read as the first."""
    from dailyfive import genres

    spec_capture["specs"] = [_spec()]
    out = director.run([{"theme": "t"}], codex, full_n=1, short_n=0, slate=SLATE)
    ledger = genres.enforce(out, SLATE)
    assert ledger["unlabelled"] == 1
    assert ledger["off_vocabulary"] == []
