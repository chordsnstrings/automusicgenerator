from datetime import date

from dailyfive.packager import (DISTRIBUTION_TEMPLATE, build_meta, slugify,
                                write_lrc)


def test_slugify_handles_punctuation_and_unicode():
    assert slugify("Slow Burn in June (Reprise)!") == "slow-burn-in-june-reprise"
    assert slugify("  Ünïcödé —  Tïtle ") == "unicode-title"
    assert slugify("") == "untitled"
    assert not slugify("a" * 200).endswith("-")


def test_lrc_groups_words_into_lines_on_pauses():
    words = [{"word": "Your", "startS": 1.0, "endS": 1.2},
             {"word": "keys", "startS": 1.25, "endS": 1.6},
             {"word": "still", "startS": 3.0, "endS": 3.3},
             {"word": "there", "startS": 3.35, "endS": 3.7}]
    out = write_lrc(words, "Slow Burn", "Vale")
    assert "[00:01.00]Your keys" in out
    assert "[00:03.00]still there" in out


def test_lrc_survives_empty_and_malformed_input():
    assert "[ti:T]" in write_lrc([], "T")
    assert "[ti:T]" in write_lrc([{"word": "", "startS": None}], "T")


def test_distribution_block_is_entirely_empty():
    meta = build_meta({"title": "T", "slot_type": "full", "duration_s": 196.0},
                      {"title": "T", "theme": "x"}, date(2026, 8, 27), 1, {})
    assert set(meta["distribution"]) == set(DISTRIBUTION_TEMPLATE)
    assert all(v in (None, [], {}) for v in meta["distribution"].values())


def test_distribution_block_is_a_copy_not_the_shared_template():
    """A shared dict would leak one song's edits into every later song."""
    meta = build_meta({"title": "T", "slot_type": "full"}, {"title": "T"},
                      date(2026, 8, 27), 1, {})
    meta["distribution"]["isrc"] = "MUTATED"
    assert DISTRIBUTION_TEMPLATE["isrc"] is None


def test_meta_records_that_the_wav_is_not_normalised():
    meta = build_meta({"title": "T", "slot_type": "full"}, {"title": "T"},
                      date(2026, 8, 27), 1, {})
    assert "not loudness normalised" in meta["mastering"]["wav"]
    assert "-14" in meta["mastering"]["mp3"]
