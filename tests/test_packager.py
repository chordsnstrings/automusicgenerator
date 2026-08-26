import shutil
import subprocess
import pytest
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


class _Recorder:
    """Stands in for packager.log.

    caplog cannot be used for this: the autouse database fixture runs alembic,
    whose fileConfig disables every logger that already existed when the test
    module was imported. caplog then captures nothing, and an assertion that
    nothing was logged at ERROR passes for the wrong reason — which is exactly
    the assertion that has to bite here.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def _at(self, level):
        return lambda msg, *a: self.calls.append((level, msg % a if a else msg))

    def __getattr__(self, name):
        return self._at(name)


def test_cover_art_declines_quietly_when_no_key_is_configured(tmp_path, monkeypatch):
    """The gap as production reported it: five ERRORs a day for a key nobody
    ever intends to set.

    ModelArkClient raises in its constructor when ARK_API_KEY is unset, so the
    old code asked for a client it already knew could not exist and then logged
    its own objection. An unconfigured optional integration is a configuration
    fact; the pipeline states it once per run instead.
    """
    from dailyfive import packager
    from dailyfive.config import reload_settings

    reload_settings()
    rec = _Recorder()
    monkeypatch.setattr(packager, "log", rec)
    assert packager.cover_art({"title": "Spent Tomorrow Twice"},
                              tmp_path / "cover.jpg") is None
    assert rec.calls == [], "an unconfigured integration is not an event"
    assert not (tmp_path / "cover.jpg").exists()


def test_an_injected_client_is_still_used_with_no_key_configured(tmp_path, monkeypatch):
    """The early return must not short-circuit a client the caller supplied —
    that is the seam every test of this function relies on."""
    from dailyfive import packager

    class FakeArk:
        def cover(self, prompt):
            return "https://example.test/cover.jpg"

    called = {}
    monkeypatch.setattr(packager, "download",
                        lambda url, dest, **kw: called.setdefault("url", url))
    out = packager.cover_art({"title": "T"}, tmp_path / "cover.jpg", client=FakeArk())
    assert out == tmp_path / "cover.jpg"
    assert called["url"] == "https://example.test/cover.jpg"


def test_a_configured_key_that_fails_is_still_an_error(tmp_path, monkeypatch):
    """The level is not being lowered across the board. A key that is set and
    stops working is a real event and must still shout."""
    from dailyfive import packager

    class Broken:
        def cover(self, prompt):
            raise RuntimeError("modelark is down")

    rec = _Recorder()
    monkeypatch.setattr(packager, "log", rec)
    assert packager.cover_art({"title": "T"}, tmp_path / "cover.jpg",
                              client=Broken()) is None
    assert [lvl for lvl, _ in rec.calls] == ["error"]
    assert "modelark is down" in rec.calls[0][1]


def test_meta_does_not_claim_a_cover_that_was_never_made(tmp_path):
    """meta.json is machine-readable, and naming a file that is not in the
    folder is a false statement in it. The key stays and is empty, exactly the
    way the distribution block reserves isrc."""
    meta = build_meta({"title": "T", "slot_type": "full"}, {"title": "T"},
                      date(2026, 8, 27), 1, {})
    assert "cover" in meta["files"]
    assert meta["files"]["cover"] is None

    made = tmp_path / "cover.jpg"
    made.write_bytes(b"\xff\xd8")
    meta = build_meta({"title": "T", "slot_type": "full"}, {"title": "T"},
                      date(2026, 8, 27), 1, {}, cover=made)
    assert meta["files"]["cover"] == "cover.jpg"


# ── the delivery master carries nothing ──────────────────────────────────────

def _tags(path):
    import subprocess
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_the_wav_master_carries_no_metadata(tmp_path):
    """Suno stamps its own name, the render time and the clip id into every WAV
    it returns, and ffmpeg copies that chunk forward unless told not to. A
    delivery master that announces where it came from is the one thing this
    file must not be."""
    from dailyfive.packager import master_wav
    from dailyfive.qc import measure

    src = tmp_path / "src.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=3", "-c:a", "pcm_s16le", "-ar", "44100",
         "-metadata", "comment=made with suno; created=2026-01-01T00:00:00Z; id=abc",
         str(src)], check=True)
    assert "suno" in _tags(src)

    out = master_wav(src, tmp_path / "master.wav", measure(src))
    assert _tags(out) == ""
    # The encoder tag is the half that looks like a failed strip: without
    # -bitexact ffmpeg empties the INFO chunk and writes its own ISFT back.
    body = out.read_bytes()
    for marker in (b"LIST", b"INFO", b"ISFT", b"ICMT", b"suno", b"Lavf"):
        assert marker not in body, marker
