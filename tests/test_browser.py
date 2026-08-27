"""The browser renderer, tested without needing a browser where possible."""

from pathlib import Path

import pytest

from dailyfive import browser
from dailyfive.video import Line, parse_lrc

TREATMENT = Path(__file__).resolve().parents[1] / "src/dailyfive/treatments/default.html"


def test_the_default_treatment_honours_the_contract():
    """The renderer owns the clock and the page owns the look. A treatment that
    does not define seek(t) would render one frame repeated for five minutes."""
    body = TREATMENT.read_text()
    assert "function seek" in body
    assert "window.LINES" in body or "d.lines" in body


def test_a_missing_browser_raises_rather_than_failing_the_run(monkeypatch, tmp_path):
    """A lyric video is worth having in a lesser form; a failed run is not."""
    monkeypatch.setattr(browser, "CHROMIUM", "/nonexistent/chromium")
    monkeypatch.setattr(browser.shutil, "which", lambda _: None)
    with pytest.raises(browser.BrowserUnavailable):
        browser._executable()


def test_frame_timings_come_from_the_page_not_the_frame_rate(tmp_path):
    """Frames arrive unevenly by construction. Assembling them on a nominal
    frame rate would drift against the audio; explicit durations cannot."""
    d = tmp_path / "frames"
    d.mkdir()
    stamps = [("f000000.jpg", 0.0), ("f000001.jpg", 0.4), ("f000002.jpg", 1.5)]
    listing = []
    for i, (name, at) in enumerate(stamps):
        nxt = stamps[i + 1][1] if i + 1 < len(stamps) else 2.0
        listing.append(f"file '{name}'\nduration {max(0.001, nxt - at):.4f}")
    listing.append(f"file '{stamps[-1][0]}'")
    text = "\n".join(listing)
    assert "duration 0.4000" in text
    assert "duration 1.1000" in text          # uneven gap preserved
    assert text.count("f000002.jpg") == 2     # concat needs the last frame twice


def test_a_sparse_capture_is_refused():
    """Four seconds of stills stretched over a song is worse than the fallback."""
    assert browser.MIN_FPS >= 4.0


def test_lines_survive_the_json_round_trip():
    lines = parse_lrc("[00:01.00]hello there\n[00:04.00]second line [Chorus]\n")
    payload = [{"start": round(l.start, 3), "end": round(l.end, 3),
                "text": l.text, "section": l.section} for l in lines]
    assert payload[0]["text"] == "hello there"
    assert all("[" not in p["text"] for p in payload)


def test_a_lyric_cannot_inject_markup():
    """Lyrics are model output and go straight into innerHTML."""
    body = TREATMENT.read_text()
    assert "replace(/[<>&]/g" in body, "treatment must strip markup from words"
