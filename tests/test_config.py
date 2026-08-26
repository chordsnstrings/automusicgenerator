"""The run shape is a setting, and zero is a legal value for a lane."""

import pytest

from dailyfive.config import reload_settings, settings
from dailyfive.errors import ConfigError


def _shape(monkeypatch, full_briefs, full_slots, short_briefs, short_slots):
    for var, value in (("FULL_BRIEFS", full_briefs), ("FULL_SLOTS", full_slots),
                       ("SHORT_BRIEFS", short_briefs), ("SHORT_SLOTS", short_slots)):
        monkeypatch.setenv(var, str(value))
    reload_settings()
    return settings()


def test_the_default_day_is_five_full_length_songs():
    """The defaults are the product: the suite neutralises the run-shape
    variables, so this is what a clone with no .env produces."""
    cfg = settings()
    assert (cfg.full_briefs, cfg.full_slots) == (7, 5)
    assert (cfg.short_briefs, cfg.short_slots) == (0, 0)
    assert (cfg.total_briefs, cfg.total_slots) == (7, 5)


def test_a_lane_with_no_slots_is_a_legal_shape(monkeypatch):
    _shape(monkeypatch, 7, 5, 0, 0).validate_shape()


def test_more_slots_than_briefs_is_not(monkeypatch):
    with pytest.raises(ConfigError):
        _shape(monkeypatch, 4, 5, 0, 0).validate_shape()
