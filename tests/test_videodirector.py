"""The shot list, and the two things it must never produce.

A loop, because that is the failure that was actually shipped and rejected: two
clips of "dancing" cut together read as one clip repeated, and a viewer sees it
at about three seconds. And a shot the generator cannot render — anything
implying she sings, anything with writing in the frame — because those come back
visibly broken rather than merely dull.
"""

from __future__ import annotations

import pytest

from dailyfive.agents import videodirector as vd
from dailyfive.cast import CAST, FIXED_TERMS, clip_prompt

P = CAST[0]


def test_consecutive_shots_never_share_a_framing():
    """The anti-loop rule is enforced, not requested.

    The model here does exactly what a cooperative model does when asked for two
    different shots: writes two different sentences about the same shot.
    """
    raw = [
        {"framing": "mid", "move": "push", "action": "she dances to the beat"},
        {"framing": "mid", "move": "drift", "action": "she keeps dancing"},
    ]
    shots = vd._repair(raw, P, shots=2)
    assert shots[0]["framing"] != shots[1]["framing"]


def test_a_long_shot_list_never_repeats_a_framing_back_to_back():
    same = [{"framing": "wide", "move": "locked", "action": "she dances"}] * 6
    shots = vd._repair(same, P, shots=6)
    for a, b in zip(shots, shots[1:]):
        assert a["framing"] != b["framing"]


def test_an_empty_answer_still_produces_a_usable_shot_list():
    """A day that ships no short because a shot list would not parse is worse
    than one that ships the ladder."""
    shots = vd._repair([], P, shots=2)
    assert len(shots) == 2
    assert all(s["framing"] in vd.FRAMINGS for s in shots)
    assert all(s["move"] in vd.MOVES for s in shots)
    assert all(s["action"] for s in shots)


def test_off_vocabulary_framing_and_move_are_replaced_not_passed_through():
    """`shot_line` indexes both dicts directly — an unknown term is a KeyError
    in the middle of a run, and a generated clip is not the place to find out."""
    raw = [{"framing": "wide angle", "move": "dolly zoom", "action": "she dances"}]
    shot = vd._repair(raw, P, shots=1)[0]
    assert shot["framing"] in vd.FRAMINGS
    assert shot["move"] in vd.MOVES
    vd.shot_line(shot)          # must not raise


@pytest.mark.parametrize("action", [
    "she sings along to the chorus",
    "she mouths the words into her hairbrush",
    "the lyrics are written on the mirror in marker",
    "a poster reads the song title behind her",
    "her phone screen shows the track name",
])
def test_a_shot_the_generator_cannot_render_is_discarded(action):
    shot = vd._repair([{"framing": "mid", "move": "push", "action": action}],
                      P, shots=1)[0]
    assert shot["action"] in vd.FALLBACK_ACTIONS


def test_an_unsafe_action_is_replaced_and_logged(caplog):
    """cast.screen() would refuse the whole video. This is upstream of it, so
    the model's bad sentence costs the sentence and not the day — but it is
    never quiet about it."""
    import logging
    with caplog.at_level(logging.WARNING):
        shot = vd._repair(
            [{"framing": "mid", "move": "push", "action": "a seductive pose in lingerie"}],
            P, shots=1)[0]
    assert shot["action"] in vd.FALLBACK_ACTIONS
    assert any("replaced" in r.getMessage() for r in caplog.records)


def test_every_repaired_shot_survives_the_cast_safety_screen():
    """Whatever this agent emits goes straight into clip_prompt, which raises on
    a forbidden term. Nothing it can emit may trip it."""
    nasty = [{"framing": "close", "move": "arc", "action": a}
             for a in ("barely legal teen dancing", "topless in the mirror", "")]
    for shot in vd._repair(nasty, P, shots=3):
        prompt = clip_prompt(P, vd.shot_line(shot), bpm=110)
        assert prompt.endswith(FIXED_TERMS)


def test_the_shot_line_does_not_redescribe_the_performer():
    """clip_prompt already carries her look. Two descriptions of the same woman
    in one prompt is how a short ends up with two different women in it."""
    line = vd.shot_line({"framing": "wide", "move": "locked",
                         "action": "she steps into the count", "why_it_follows": ""})
    for word in ("hair", "skin", "wearing", "woman", "earrings"):
        assert word not in line.lower()


def test_the_shot_line_reads_as_one_sentence_about_her():
    line = vd.shot_line({"framing": "mid", "move": "push",
                         "action": "she turns on the last beat", "why_it_follows": ""})
    assert line.startswith(vd.FRAMINGS["mid"])
    assert "She turns on the last beat" in line
    assert "She she" not in line


def test_the_role_is_registered_so_it_can_be_pointed_at_a_brain():
    from dailyfive import llm
    assert "videodirector" in llm.ROLES
    assert "videodirector" in llm.ROLE_HINTS
    assert llm.roster()["videodirector"]


def test_plan_falls_back_rather_than_failing_the_day(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("no brain configured")
    monkeypatch.setattr(vd, "ask_json", boom)
    shots = vd.plan({"title": "x"}, P, shots=2, bpm=118)
    assert len(shots) == 2
    assert shots[0]["framing"] != shots[1]["framing"]


def test_plan_passes_the_performers_energy_to_the_model(monkeypatch):
    """Directing a stop-and-go dancer into flowing continuous movement is
    directing against the only person in the frame."""
    seen = {}

    def fake(role, system, user, **kw):
        seen["user"] = user
        return {"shots": [{"framing": "wide", "move": "locked", "action": "she counts in"},
                          {"framing": "close", "move": "push", "action": "she turns"}]}

    monkeypatch.setattr(vd, "ask_json", fake)
    vd.plan({"title": "Song", "theme": "a specific situation", "hook_note": "0:07"},
            CAST[1], shots=2, bpm=128)
    assert CAST[1].energy in seen["user"]
    assert "128 BPM" in seen["user"]
