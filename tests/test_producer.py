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
