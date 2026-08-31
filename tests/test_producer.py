"""The slot contract is not the model's to renegotiate."""

from dailyfive.agents.producer import _honour_slots, _view


def _pool():
    return {
        "full": [{"clip_id": i, "score_total": 10 - i} for i in range(4)],
        "short": [{"clip_id": 10 + i, "score_total": 5 - i} for i in range(3)],
    }


def test_model_overpicking_is_capped():
    picks = _honour_slots(
        [{"clip_id": i, "slot_type": "full"} for i in range(4)],
        _pool(), full_slots=3, short_slots=2)
    assert sum(1 for p in picks if p["slot_type"] == "full") == 3
    assert sum(1 for p in picks if p["slot_type"] == "short") == 2


def test_invalid_clip_ids_are_ignored():
    picks = _honour_slots([{"clip_id": 999, "slot_type": "full"}],
                          _pool(), full_slots=3, short_slots=2)
    assert 999 not in {p["clip_id"] for p in picks}
    assert len(picks) == 5


def test_no_model_output_falls_back_to_score_order():
    picks = _honour_slots([], _pool(), full_slots=3, short_slots=2)
    full = [p["clip_id"] for p in picks if p["slot_type"] == "full"]
    assert full == [0, 1, 2]


def test_a_short_candidate_cannot_fill_a_full_slot():
    picks = _honour_slots([{"clip_id": 10, "slot_type": "full"}],
                          _pool(), full_slots=3, short_slots=2)
    full = [p["clip_id"] for p in picks if p["slot_type"] == "full"]
    assert 10 not in full


def test_duplicate_picks_are_collapsed():
    picks = _honour_slots([{"clip_id": 0, "slot_type": "full"}] * 3,
                          _pool(), full_slots=3, short_slots=2)
    assert len({p["clip_id"] for p in picks}) == len(picks)


def test_a_lane_with_no_slots_takes_nothing():
    picks = _honour_slots([{"clip_id": 10, "slot_type": "short"}],
                          _pool(), full_slots=3, short_slots=0)
    assert [p["slot_type"] for p in picks] == ["full"] * 3


def test_mix_scorer_never_sees_the_lyric():
    """Otherwise a good lyric inflates the mix score — the anchoring this avoids."""
    c = {"clip_id": 1, "title": "T", "slot_type": "full",
         "lyrics": "a wonderful lyric", "qc": {"lufs_i": -12}}
    assert "lyrics" not in _view(c, "mix")
    assert "lyric_opening" in _view(c, "hook")
    assert "qc" not in _view(c, "hook")


# ── one clip per brief ───────────────────────────────────────────────────────
def _pair(brief_id, a, b, score_a, score_b):
    """Two takes of one song, the way Suno actually returns them."""
    return [
        {"clip_id": a, "brief_id": brief_id, "slot_type": "full", "score_total": score_a},
        {"clip_id": b, "brief_id": brief_id, "slot_type": "full", "score_total": score_b},
    ]


def _by_type(clips):
    from collections import defaultdict
    out = defaultdict(list)
    for c in clips:
        out[c["slot_type"]].append(c)
    for group in out.values():
        group.sort(key=lambda c: c["score_total"], reverse=True)
    return dict(out)


def test_both_takes_of_one_song_never_fill_two_slots():
    """2026-08-30: slots 1 and 2 were both "I Won't Carry You to the Floor" at
    8.25 and 8.07, a fifth distinct song sat held at 6.35, and the written
    rationale claimed "five leading themes with one track each". Four songs
    shipped that day, one of them twice."""
    from dailyfive.agents.producer import _honour_slots

    clips = (_pair(1, 101, 102, 8.25, 8.07) + _pair(2, 103, 104, 7.45, 7.08)
             + _pair(3, 105, 106, 6.92, 6.85) + _pair(4, 107, 108, 6.95, 6.67)
             + _pair(5, 109, 110, 6.35, 6.12))
    # The model asks for both takes of its favourite, exactly as it did.
    model = [{"clip_id": c, "slot_type": "full"} for c in (101, 102, 103, 105, 107)]

    picks = _honour_slots(model, _by_type(clips), full_slots=5, short_slots=0)
    assert len(picks) == 5
    briefs = [next(c["brief_id"] for c in clips if c["clip_id"] == p["clip_id"])
              for p in picks]
    assert len(set(briefs)) == 5, f"shipped the same song twice: {briefs}"
    # And the slot the duplicate wanted goes to the fifth distinct song.
    assert 109 in {p["clip_id"] for p in picks}


def test_the_better_take_of_a_pair_is_the_one_that_ships():
    from dailyfive.agents.producer import _honour_slots
    clips = _pair(1, 101, 102, 8.25, 8.07) + _pair(2, 103, 104, 7.4, 7.1)
    picks = _honour_slots([], _by_type(clips), full_slots=2, short_slots=0)
    assert {p["clip_id"] for p in picks} == {101, 103}


def test_a_short_field_ships_a_second_take_rather_than_an_empty_slot():
    """Five releases where one pair is two takes is a worse day than five
    distinct songs and a better one than four releases."""
    from dailyfive.agents.producer import _honour_slots
    clips = _pair(1, 101, 102, 8.0, 7.9) + _pair(2, 103, 104, 7.0, 6.9)
    picks = _honour_slots([], _by_type(clips), full_slots=3, short_slots=0)
    assert len(picks) == 3
    assert {p["clip_id"] for p in picks} == {101, 103, 102}


def test_a_candidate_with_no_brief_id_is_never_called_a_duplicate():
    """An unknown pairing is not evidence of one, and guessing would drop a
    song rather than a repeat."""
    from dailyfive.agents.producer import _honour_slots
    clips = [{"clip_id": 1, "slot_type": "full", "score_total": 9.0},
             {"clip_id": 2, "slot_type": "full", "score_total": 8.0}]
    picks = _honour_slots([], _by_type(clips), full_slots=2, short_slots=0)
    assert len(picks) == 2


def test_the_producer_can_see_the_pairing_at_all():
    """The root cause was that it could not: brief_id was absent from the
    candidate dict, so two takes were indistinguishable from two songs."""
    import inspect

    from dailyfive import pipeline
    from dailyfive.agents import producer
    assert '"brief_id"' in inspect.getsource(pipeline._clip_dict)
    assert '"brief_id": c.get("brief_id")' in inspect.getsource(producer._select)
