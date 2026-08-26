"""The Scout's genre output is a controlled term by the time anyone reads it.

``sonic_calibration.genre_mix`` is free text from a model, and it is the only
thing on the signal sheet the genre allocator reads. A synonym there is a genre
with no history behind it, so the mapping happens once, here, deterministically,
rather than being asked for and hoped for.
"""

from __future__ import annotations

import pytest

from dailyfive.agents import scout
from dailyfive.signals import FeedResult


@pytest.fixture
def answered(monkeypatch):
    box = {"result": {}, "user": ""}

    def fake_ask_json(role, system, user, *, schema_hint=None, **kw):
        box["user"] = user
        return box["result"]

    monkeypatch.setattr(scout, "ask_json", fake_ask_json)
    return box


THEME = {"theme": "the drive home after saying it", "sentiment": "flat",
         "lead": "leading", "confidence": 0.6}

FEEDS = [FeedResult("apple", "lagging", items=[
    {"rank": 1, "title": "x", "genres": ["Country"], "genre_ids": ["6"],
     "released": "2026-06-01", "window": "current"}])]


def test_the_genre_mix_arrives_in_the_vocabulary(answered):
    answered["result"] = {"themes": [THEME], "sonic_calibration": {
        "genre_mix": ["Country Pop", "R&B/Soul", "hip hop"]}}
    sheet = scout.run("US", feeds=FEEDS)
    assert sheet["sonic_calibration"]["genre_mix"] == ["country", "r-and-b", "hip-hop"]


def test_a_name_outside_the_vocabulary_is_kept_in_the_model_s_own_words(answered):
    """Reconciling an outside label with ours is judgement, and it belongs to
    the weekly retro, which proposes and never writes. Guessing here would put
    a fabricated label into the column the whole record is keyed on."""
    answered["result"] = {"themes": [THEME], "sonic_calibration": {
        "genre_mix": ["afrobeats", "country"]}}
    cal = scout.run("US", feeds=FEEDS)["sonic_calibration"]
    assert cal["genre_mix"] == ["country"]
    assert cal["genre_mix_outside_vocabulary"] == ["afrobeats"]


def test_the_same_family_twice_is_one_entry(answered):
    answered["result"] = {"themes": [THEME], "sonic_calibration": {
        "genre_mix": ["country", "Country-Folk", "outlaw country"]}}
    assert scout.run("US", feeds=FEEDS)["sonic_calibration"]["genre_mix"] == ["country"]


def test_a_missing_calibration_block_is_not_an_error(answered):
    answered["result"] = {"themes": [THEME]}
    cal = scout.run("US", feeds=FEEDS)["sonic_calibration"]
    assert cal["genre_mix"] == [] and cal["genre_mix_outside_vocabulary"] == []


def test_the_counted_chart_reaches_the_prompt_and_the_sheet(answered):
    """The Scout used to be handed fifty rows and left to count them, which is
    how a chart that is 62% catalogue reads as a genre being released."""
    answered["result"] = {"themes": [THEME], "sonic_calibration": {}}
    sheet = scout.run("US", feeds=FEEDS)
    assert sheet["external_genres"]["current"] == {"country": 1}
    assert "genre_counts" in answered["user"]
    assert "genre_vocabulary" in answered["user"]
