"""Clearance, and the cost of saying no.

A rejected brief is a song that does not get made. Nothing replaces it, the day
ships one fewer, and — until the Producer was taught otherwise — the freed slot
went to a second take of another song. So the interesting property here is not
that it blocks things, it is that it blocks only what an edit cannot fix.
"""

from __future__ import annotations

from dailyfive.agents import clearance

BRIEF = {"title": "One More Season, You Promised", "theme": "a fan writes to a showrunner"}
LYRICS = "[Verse]\nyou said the show was over\n[Chorus]\none more time\none more time"
STYLE = "uptempo synth-pop, 118 BPM, analog saw lead, four-on-floor kick"


def _model(monkeypatch, payload):
    monkeypatch.setattr(clearance, "ask_json", lambda *a, **k: payload)


def test_a_common_phrase_hook_never_costs_the_day_a_song(monkeypatch, caplog):
    """2026-08-30: rejected because its hook was "one more time". A repeated
    common phrase is how choruses are built — it belongs to nobody, and
    repetition is what a chorus IS."""
    import logging

    _model(monkeypatch, {
        "verdict": "reject",
        "reasons": ["The phrase 'one more time' is used as a recurring hook"],
    })
    with caplog.at_level(logging.WARNING):
        out = clearance.run(BRIEF, LYRICS, STYLE)
    assert out["verdict"] != "reject"
    # And it is never quiet about overriding the model.
    assert any("model judgement alone" in r.getMessage() for r in caplog.records)


def test_a_downgraded_reject_that_proposes_no_fix_becomes_a_pass(monkeypatch):
    """A rewrite that changed nothing is a pass with extra steps — the same
    answer the rules pass gives on its own. What is no longer available is
    spending a slot on a judgement no rule agreed with."""
    _model(monkeypatch, {"verdict": "reject", "reasons": ["felt risky"]})
    assert clearance.run(BRIEF, LYRICS, STYLE)["verdict"] == "pass"


def test_a_downgraded_reject_still_takes_the_edit_it_proposed(monkeypatch):
    """There may well be something worth changing — the model thought so — so
    the fix it offered is applied rather than discarded with the verdict."""
    _model(monkeypatch, {
        "verdict": "reject",
        "reasons": ["chorus is close to an existing song"],
        "lyrics_fixed": "[Verse]\nyou said the show was over\n[Chorus]\nonce more, then\nonce more, then",
    })
    out = clearance.run(BRIEF, LYRICS, STYLE)
    assert out["verdict"] == "rewrite"
    assert "once more, then" in out["lyrics"]
    assert "one more time" not in out["lyrics"]


def test_a_hard_rule_hit_still_rejects(monkeypatch):
    """The downgrade is only for a judgement call. A deterministic rule that
    fired is not a judgement call, and a named stylistic target is certain to be
    refused by the generator anyway."""
    style = "in the style of Taylor Swift, uptempo synth-pop"
    hits = clearance._rules_pass(LYRICS, style)
    assert any(h["severity"] == "high" for h in hits), "expected a hard rule hit"

    out = clearance.run(BRIEF, LYRICS, style, use_model=False)
    assert out["verdict"] == "reject"


def test_the_prompt_says_what_reject_is_for():
    assert "REJECT IS FOR WHAT AN EDIT CANNOT FIX" in clearance.SYSTEM
    assert "belong to nobody" in clearance.SYSTEM
