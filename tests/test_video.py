"""The lyric video and the short, which share a parser and nothing else."""

import pytest

from dailyfive.cast import CAST, FIXED_TERMS, UnsafeBrief, clip_prompt, pick, screen
from dailyfive.video import beat_grid, build_ass, hook_window, parse_lrc

# The shape the Packager actually writes, including the welded section tags
# that a naive parser leaves inside the lyric.
REAL_LRC = """[ti:The Door at the Crematorium]
[ar:Vale]
[00:02.71][Intro]
Let me in Let me in
[00:17.03]in Let me in
[00:28.04][Verse]
I brought his coat
[01:20.31]in two days [Pre]
[01:36.82]in [Chorus]
Let me in, the lock is on the inside
[02:10.45]I am not a stranger [Verse]
"""


def test_a_welded_section_tag_never_reaches_the_screen():
    """The tags sit at the end of a lyric line, not alone on one. Missing that
    puts the word "[Chorus]" on screen and collapses the slide count."""
    lines = parse_lrc(REAL_LRC)
    assert lines, "parsed nothing"
    for ln in lines:
        assert "[" not in ln.text and "]" not in ln.text, ln.text


def test_every_section_after_the_second_is_still_found():
    lines = parse_lrc(REAL_LRC)
    found = {ln.section for ln in lines if ln.section}
    assert {"Intro", "Verse", "Pre", "Chorus"} <= found, found


def test_a_tag_names_what_follows_not_the_line_carrying_it():
    lines = parse_lrc(REAL_LRC)
    welded = next(ln for ln in lines if ln.text == "in two days")
    assert welded.section == "Verse", "the Pre starts after this line, not on it"


def test_lines_do_not_overlap():
    lines = parse_lrc(REAL_LRC)
    for a, b in zip(lines, lines[1:]):
        assert a.end <= b.start + 1e-6, f"{a.text!r} overruns {b.text!r}"


def test_the_hook_is_the_chorus_not_the_opening():
    """A short has two seconds to earn the next twenty, and these songs open on
    an intro nobody stays for."""
    lines = parse_lrc(REAL_LRC)
    start, end = hook_window(lines, want_s=20.0)
    assert start > 90.0, start
    assert end - start == pytest.approx(20.0)


def test_the_subtitle_file_is_sized_from_the_frame():
    lines = parse_lrc(REAL_LRC)
    land = build_ass(lines, width=1920, height=1080)
    port = build_ass(lines, width=1080, height=1920)
    assert "PlayResY: 1080" in land and "PlayResY: 1920" in port
    # A point size that reads on a television is unreadable on a phone held at
    # arm's length, so the two must not come out the same.
    assert land.split("Style: Lyric,")[1] != port.split("Style: Lyric,")[1]


def test_a_brace_in_a_lyric_cannot_open_an_override_block():
    lines = parse_lrc("[00:01.00]a {weird} lyric\n")
    body = build_ass(lines, width=1080, height=1920)
    assert "{weird}" not in body
    assert "(weird)" in body


def test_the_beat_grid_comes_from_the_brief_not_a_waveform():
    grid = beat_grid(120, 8.0)                 # 120bpm, a bar is 2s
    assert grid == [2.0, 4.0, 6.0]
    assert beat_grid(0, 10.0) == []


# ── the terms that are not negotiable ────────────────────────────────────────

def test_every_clip_prompt_carries_the_fixed_terms_last():
    """Appended after the caller's text, so a later instruction cannot be
    argued out of by an earlier one."""
    p = CAST[0]
    out = clip_prompt(p, "she steps into the light and turns", bpm=96)
    assert out.endswith(FIXED_TERMS)


def test_a_forbidden_brief_is_refused_rather_than_sanitised():
    with pytest.raises(UnsafeBrief):
        screen("a teenage girl dancing")
    with pytest.raises(UnsafeBrief):
        clip_prompt(CAST[0], "seductive pose in lingerie")


def test_casting_is_deterministic_so_a_rerun_recasts_the_same_person():
    a = pick(run_date="2026-08-27", clip_id=41)
    b = pick(run_date="2026-08-27", clip_id=41)
    assert a is b
    assert pick(run_date="2026-08-28", clip_id=41) in CAST


def test_no_performer_description_carries_its_own_age_term():
    """Age is stated once, in FIXED_TERMS, so it cannot drift per performer."""
    for p in CAST:
        low = p.look.lower()
        for word in ("young", "teen", "girl", "age", "year"):
            assert word not in low, f"{p.key}: {word}"
