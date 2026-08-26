"""The Compiler is the last place a mistake is cheap, so it gets the most tests."""

import pytest

from dailyfive.agents.compiler import compile_payload, strip_internal, validate
from dailyfive.errors import ConfigError

BASE = {
    "title": "Slow Burn in June",
    "slot_type": "full",
    "style_string": "dark rnb, 808s, close-mic vocal",
    "lyrics": "[Verse]\nYour keys still on the hook\n[Chorus]\nCome back through the door",
    "vocal_gender": "f",
}


def test_valid_payload_round_trips():
    payload = compile_payload(BASE)
    assert validate(strip_internal(payload)) == []
    assert payload["customMode"] is True
    assert payload["instrumental"] is False
    assert payload["callBackUrl"].startswith("https://songs.test/webhooks/")


def test_style_truncated_to_model_limit_and_reported():
    long_style = ", ".join(["a descriptor"] * 200)
    payload = compile_payload({**BASE, "style_string": long_style}, model="V4")
    assert len(payload["style"]) <= 200
    assert any("truncated" in w for w in payload["_warnings"])
    assert validate(strip_internal(payload)) == []


def test_v4_5_allows_a_far_longer_style_than_v4():
    long_style = ", ".join(["a descriptor"] * 200)
    v4 = compile_payload({**BASE, "style_string": long_style}, model="V4")
    v45 = compile_payload({**BASE, "style_string": long_style}, model="V4_5")
    assert len(v45["style"]) > len(v4["style"])


def test_duration_only_on_v5_5():
    short = {**BASE, "slot_type": "short"}
    assert "duration" in compile_payload(short, model="V5_5")
    v4 = compile_payload(short, model="V4")
    assert "duration" not in v4
    assert any("does not accept duration" in w for w in v4["_warnings"])


def test_duration_on_wrong_model_is_caught_by_validate():
    bad = {**compile_payload(BASE, model="V4"), "duration": 45}
    assert any("duration is only valid" in p for p in validate(strip_internal(bad)))


def test_missing_style_is_fatal():
    with pytest.raises(ConfigError, match="no style string"):
        compile_payload({**BASE, "style_string": ""})


def test_missing_lyrics_is_fatal():
    with pytest.raises(ConfigError, match="no lyrics"):
        compile_payload({**BASE, "lyrics": ""})


def test_unknown_model_is_fatal():
    with pytest.raises(ConfigError, match="unknown Suno model"):
        compile_payload(BASE, model="V9_9")


def test_weights_are_clamped():
    p = compile_payload(BASE, style_weight=5.0, weirdness=-2.0, audio_weight="x")
    assert p["styleWeight"] == 1.0
    assert p["weirdnessConstraint"] == 0.0
    assert 0.0 <= p["audioWeight"] <= 1.0
    assert validate(strip_internal(p)) == []


def test_invalid_vocal_gender_is_dropped_not_sent():
    p = compile_payload({**BASE, "vocal_gender": "nonbinary"})
    assert "vocalGender" not in p
    assert any("vocalGender" in w for w in p["_warnings"])


def test_persona_without_id_warns_rather_than_silently_generic():
    p = compile_payload({**BASE, "persona_name": "Vale", "persona_id": None})
    assert "personaId" not in p
    assert any("generic voice" in w for w in p["_warnings"])


def test_persona_defaults_to_style_persona():
    p = compile_payload({**BASE, "persona_id": "psn_1"})
    assert p["personaModel"] == "style_persona"
    assert validate(strip_internal(p)) == []


def test_strip_internal_removes_bookkeeping_keys():
    p = compile_payload(BASE)
    assert "_warnings" in p
    assert not any(k.startswith("_") for k in strip_internal(p))


def test_truncation_prefers_a_clause_boundary():
    style = "alpha bravo, charlie delta, echo foxtrot, " + "x" * 300
    p = compile_payload({**BASE, "style_string": style}, model="V4")
    assert not p["style"].endswith(",")
