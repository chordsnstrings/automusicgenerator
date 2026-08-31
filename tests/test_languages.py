"""The second language, and the check that it will be legible.

The whole point of this module is that a rendering failure here is invisible.
A script with no font burns into the lyric video as tofu boxes with ffmpeg
exiting zero, the file decoding, and the duration correct — so the guarantee has
to be structural, not a comment.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dailyfive import languages


# ── the render gate ──────────────────────────────────────────────────────────
def test_every_offered_language_can_actually_be_rendered():
    """The one test that must never be relaxed by deleting a language from the
    assertion. If this fails, either add the font package to the Dockerfile or
    take the language out of the roster — never the other way round."""
    missing = [lang.name for lang in languages.LANGUAGES
               if not languages.renderable(lang)]
    assert not missing, (
        f"no font covers {', '.join(missing)} on this machine. The Dockerfile "
        f"installs fonts-dejavu-core, fonts-nanum and fonts-noto-core; a "
        f"language outside their coverage must not be in the roster.")


def test_a_script_with_no_font_is_refused(tmp_path, monkeypatch):
    """Proved by restricting fontconfig to one font rather than by trusting the
    machine to be missing something — this sandbox carries unifont, which covers
    almost every script and would make any negative test pass by accident."""
    import shutil
    import subprocess

    dejavu = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not __import__("pathlib").Path(dejavu).is_file():
        pytest.skip("DejaVu not present to build a restricted font set from")

    fonts = tmp_path / "fonts"
    fonts.mkdir()
    shutil.copy(dejavu, fonts / "DejaVuSans.ttf")
    conf = tmp_path / "fonts.conf"
    conf.write_text(
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd">'
        f"<fontconfig><dir>{fonts}</dir>"
        f"<cachedir>{tmp_path / 'cache'}</cachedir></fontconfig>")
    (tmp_path / "cache").mkdir()

    korean = languages.BY_CODE["ko"]
    probe = subprocess.run(
        ["fc-list", f":charset={languages._charset(korean.sample)}"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "FONTCONFIG_FILE": str(conf)})
    assert not probe.stdout.strip(), (
        "DejaVu alone should not cover Hangul — this is exactly the production "
        "state that would have shipped Korean lyrics as tofu")


def test_no_font_tooling_means_no_language_rather_than_a_guess(monkeypatch):
    """An environment that cannot answer the question is not one to take a
    chance in: the cost of a wrong yes is a delivered file of boxes."""
    monkeypatch.setattr(languages.shutil, "which", lambda _n: None)
    assert not languages.renderable(languages.BY_CODE["es"])
    assert languages.available() == ()


# ── allocation ───────────────────────────────────────────────────────────────
def test_the_floor_is_actually_allocated():
    got = languages.assign(7, floor=2, run_date=date(2026, 9, 1))
    assert len(got) == 2
    assert all(isinstance(lang, languages.Language) for lang, _p in got.values())


def test_a_day_never_gets_the_same_language_twice():
    for k in range(40):
        got = languages.assign(7, floor=2, run_date=date(2026, 9, 1) + timedelta(days=k))
        codes = [lang.code for lang, _p in got.values()]
        assert len(codes) == len(set(codes)), codes


def test_the_same_day_always_allocates_the_same_languages():
    """Deterministic, so a re-run after a crash rewrites the same brief rather
    than a different one, and the console can say why without answering 'a coin
    came up'."""
    a = languages.assign(7, floor=2, run_date=date(2026, 9, 3))
    b = languages.assign(7, floor=2, run_date=date(2026, 9, 3))
    assert [(l.code, p) for l, p in a.values()] == [(l.code, p) for l, p in b.values()]


def test_the_roster_is_sampled_evenly():
    """The point of recording the language is to find out which ones travel. A
    roster sampled unevenly answers that question about the sampling."""
    from collections import Counter
    seen = Counter()
    for k in range(len(languages.LANGUAGES) * 6):
        for lang, _p in languages.assign(
                7, floor=2, run_date=date(2026, 9, 1) + timedelta(days=k)).values():
            seen[lang.code] += 1
    assert len(seen) == len(languages.LANGUAGES)
    assert max(seen.values()) - min(seen.values()) <= 1, seen


def test_a_language_does_not_always_land_in_the_same_placement():
    """The language rotation alone has a period equal to the roster size, so a
    placement derived only from the day would share it — Spanish would be the
    rap verse every single time."""
    pairs = set()
    for k in range(len(languages.LANGUAGES) * len(languages.PLACEMENTS) + 5):
        for lang, placement in languages.assign(
                7, floor=2, run_date=date(2026, 9, 1) + timedelta(days=k)).values():
            pairs.add((lang.code, placement))
    per_language = {}
    for code, placement in pairs:
        per_language.setdefault(code, set()).add(placement)
    assert all(len(v) > 1 for v in per_language.values()), per_language


def test_a_floor_of_zero_ships_an_all_english_day():
    assert languages.assign(7, floor=0, run_date=date(2026, 9, 1)) == {}


def test_the_floor_cannot_exceed_the_briefs():
    got = languages.assign(2, floor=5, run_date=date(2026, 9, 1))
    assert len(got) == 2


def test_an_empty_roster_ships_english_rather_than_tofu():
    """A container missing its font packages must degrade to English songs, not
    to boxes."""
    assert languages.assign(7, floor=2, run_date=date(2026, 9, 1), roster=()) == {}


# ── what the lyricist is told ────────────────────────────────────────────────
def test_the_brief_note_forbids_translating_the_english():
    note = languages.brief_note(languages.BY_CODE["es"], "rap verse")
    assert "rap verse" in note
    assert "Spanish" in note
    assert "Do not translate" in note
    assert "Do not romanise" in note


def test_the_brief_note_names_the_script_so_hangul_is_not_romanised():
    note = languages.brief_note(languages.BY_CODE["ko"], "rap verse")
    assert "Hangul" in note


def test_arabic_is_marked_right_to_left():
    assert languages.BY_CODE["ar"].rtl
    assert not languages.BY_CODE["es"].rtl


def test_japanese_is_not_offered():
    """Taken out because renderable() tests a sample, and kanji is the one
    script in reach where a sample proves nothing: the six-character sample was
    covered and an ordinary kanji line was not."""
    assert "ja" not in languages.BY_CODE


def test_a_shipped_language_code_is_never_renamed():
    """Same rule as the genre labels: a rename orphans every row carrying the
    old value and splits one track record into two halves."""
    assert {"es", "fr", "pt", "ko", "ar"} <= set(languages.BY_CODE)


# ── the chain, end to end ────────────────────────────────────────────────────
def test_the_language_reaches_the_lyricist_prompt():
    from dailyfive.agents.lyricist import _brief_prompt
    prompt = _brief_prompt({"title": "T", "theme": "x", "slot_type": "full",
                            "language": "ko", "language_placement": "rap verse"})
    assert "Korean" in prompt and "rap verse" in prompt and "Hangul" in prompt


def test_an_english_song_gets_no_language_instruction_at_all():
    from dailyfive.agents.lyricist import _brief_prompt
    prompt = _brief_prompt({"title": "T", "theme": "x", "slot_type": "full"})
    for word in ("Spanish", "Korean", "Arabic", "ONE SECTION"):
        assert word not in prompt


def test_the_second_draft_is_told_to_keep_the_language():
    """The variation instruction reads as licence to write a different song, and
    the foreign section is the first thing a rewrite loses."""
    from dailyfive.agents.lyricist import _variation
    assert "Keep the second-language section" in _variation(2, "full", has_language=True)
    assert "Keep the second-language section" not in _variation(2, "full")


def test_the_style_string_steers_the_voice_not_just_the_lyric():
    """Suno reads the foreign script in the lyric, but the style field is what
    steers pronunciation — without it the verse comes back sung with English
    phonology."""
    from dailyfive.agents.compiler import compile_payload
    payload = compile_payload({
        "title": "T", "slot_type": "full", "style_string": "uptempo alt-pop, 808s",
        "lyrics": "[Verse]\nline\n[Chorus]\nhook",
        "language": "ko", "language_placement": "rap verse"})
    assert "Korean" in payload["style"]
    assert "uptempo alt-pop" in payload["style"]


def test_an_english_song_gets_an_untouched_style_string():
    from dailyfive.agents.compiler import compile_payload
    payload = compile_payload({
        "title": "T", "slot_type": "full", "style_string": "uptempo alt-pop, 808s",
        "lyrics": "[Verse]\nline"})
    assert payload["style"] == "uptempo alt-pop, 808s"


def test_a_foreign_lyric_survives_the_lrc_round_trip():
    """The .lrc is what the lyric video reads. A parser that mangles non-Latin
    text would be a rendering failure one step earlier than the font."""
    from dailyfive.video import parse_lrc
    lrc = ("[00:01.00]I kept the photo\n"
           "[00:04.00]네가 없는 밤이 제일 길어 [Chorus]\n"
           "[00:07.00]ما زلت أنتظر على الرصيف\n"
           "[00:10.00]no me llames si no vas a quedarte\n")
    lines = parse_lrc(lrc)
    assert [l.text for l in lines] == [
        "I kept the photo", "네가 없는 밤이 제일 길어",
        "ما زلت أنتظر على الرصيف", "no me llames si no vas a quedarte"]
    # A welded tag marks where the NEXT section starts, so the Chorus label
    # lands on the line after the one carrying it. Asserted on a non-Latin line
    # because the stripping and the labelling are separate steps and only the
    # first is obviously script-agnostic.
    assert lines[2].section == "Chorus"


def test_a_foreign_lyric_survives_the_ass_subtitle_build():
    """build_ass writes the file libass burns. Nothing may escape or drop a
    non-Latin character on the way in."""
    from dailyfive.video import build_ass, parse_lrc
    lines = parse_lrc("[00:01.00]네가 없는 밤이\n[00:04.00]ما زلت أنتظر\n")
    ass = build_ass(lines, width=1920, height=1080)
    assert "네가 없는 밤이" in ass
    assert "ما زلت أنتظر" in ass
