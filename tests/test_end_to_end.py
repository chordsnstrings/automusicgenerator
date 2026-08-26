"""One full run with every external call faked.

This is the test that proves the eleven pieces actually fit together. Nothing
here reaches the network, spends a credit, or needs ffmpeg — but every phase
transition, every database write and every hand-off between agents is real.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dailyfive import pipeline as pl
from dailyfive.config import settings
from dailyfive.db import session_scope
from dailyfive.models import Brief, Clip, Decision, Job, Run, RunPhase, Signal
from dailyfive.qc import QCMetrics

THEMES = [
    {"rank": i + 1, "theme": f"situation number {i}", "sentiment": "wistful",
     "evidence": "gtrends + reddit", "sources": ["gtrends"], "lead": "leading",
     "confidence": 0.7}
    for i in range(10)
]


class FakeSuno:
    def __init__(self):
        self.tasks = 0
        self.wav_calls = 0

    def credits(self):
        return 5000

    def generate(self, payload):
        self.tasks += 1
        return f"task-{self.tasks}"

    def record_info(self, task_id):
        n = task_id.split("-")[1]
        return {"status": "SUCCESS", "response": {"sunoData": [
            {"id": f"audio-{n}-{v}", "audio_url": f"https://fake/{n}-{v}.mp3",
             "title": f"Song {n}-{v}", "duration": 190.0 + v, "tags": "rnb"}
            for v in (0, 1)]}}

    def wav_generate(self, task_id, audio_id):
        self.wav_calls += 1
        return f"wav-{self.wav_calls}"

    def wav_record_info(self, wav_task_id):
        return {"successFlag": "SUCCESS",
                "response": {"audioWavUrl": f"https://fake/{wav_task_id}.wav"}}

    def timestamped_lyrics(self, task_id, audio_id):
        return [{"word": "line", "startS": 1.0, "endS": 1.4},
                {"word": "two", "startS": 3.0, "endS": 3.4}]


class FakeSpaces:
    def __init__(self):
        self.objects: dict[str, str] = {}
        self.prefix = "songs"

    def key_for(self, run_date, *parts):
        return "/".join([self.prefix, run_date, *[p.strip("/") for p in parts if p]])

    def upload(self, local, key, public=False, clip_id=None, run_id=None):
        self.objects[key] = str(local)
        return key

    def put_text(self, body, key, content_type="application/json", public=False,
                 clip_id=None, run_id=None):
        self.objects[key] = body
        return key

    def signed_url(self, key, expires=0):
        return f"https://spaces.fake/{key}?sig=x"

    def check_access(self):
        return None


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Replace every boundary with a fake, leaving all the wiring real."""
    from dailyfive.agents import anr, clearance, director, lyricist, producer, scout

    monkeypatch.setattr(scout, "run", lambda region, want=10: {
        "themes": THEMES, "sonic_calibration": {"tempo_centre": 96},
        "feeds_live": ["gtrends", "deezer"], "feeds_dead": {}})

    def fake_director(themes, codex, *, full_n, short_n, calibration=None):
        specs = []
        for i in range(full_n):
            specs.append({"theme": themes[i]["theme"], "slot_type": "full", "bpm": 84 + i,
                          "key": "F minor", "song_form": "Verse - Chorus",
                          "instrumentation": "sub bass, hats", "hook_note": "hook at 0:07",
                          "vocal_gender": "f" if i % 2 else "m",
                          "style_string": f"dark rnb, 808s, variant {i}",
                          "mix_note": "vocal forward"})
        for i in range(short_n):
            specs.append({"theme": themes[full_n + i]["theme"], "slot_type": "short",
                          "bpm": 120 + i, "key": "A minor", "song_form": "Hook - Verse",
                          "instrumentation": "saw lead", "hook_note": "hook at 0:00",
                          "vocal_gender": "f",
                          "style_string": f"club, sidechain, variant {i}",
                          "mix_note": "loud"})
        return specs
    monkeypatch.setattr(director, "run", fake_director)

    monkeypatch.setattr(anr, "run", lambda specs, codex, history_days=14: [
        {**sp, "idx": i, "title": f"Track {i}", "persona_name": "Vale",
         "persona_id": "psn_fake", "persona_model": "style_persona",
         "angle": f"angle {i}", "diversity_vector": {"mood": f"m{i}",
                                                     "tempo_band": "midtempo",
                                                     "person": "first",
                                                     "subject": f"s{i}"}}
        for i, sp in enumerate(specs)])

    monkeypatch.setattr(lyricist, "run", lambda brief, client=None: {
        "lyrics": "[Verse]\nyour keys still on the hook\n[Chorus]\ncome back through",
        "lyric_hash": f"h{brief['title']}", "draft_chosen": 1,
        "draft_count": 2, "lint": []})

    monkeypatch.setattr(clearance, "run", lambda brief, lyrics, style, use_model=True: {
        "verdict": "pass", "reasons": [], "lyrics": lyrics, "style_string": style,
        "rules_hits": [], "severity": "none"})

    def fake_producer(candidates, signals, *, full_slots, short_slots):
        for i, c in enumerate(candidates):
            c["score_total"] = 9.0 - i * 0.1
        return producer._select(candidates, signals, full_slots=full_slots,
                                short_slots=short_slots) | {
            "scores": {c["clip_id"]: {"hook": 8.0, "mix": 7.0, "trend": 9.0,
                                      "total": c["score_total"]} for c in candidates}}
    monkeypatch.setattr(producer, "run", fake_producer)

    fake_suno = FakeSuno()
    fake_spaces = FakeSpaces()
    monkeypatch.setattr(pl, "SunoClient", lambda *a, **kw: fake_suno)
    monkeypatch.setattr(pl, "open_store", lambda *a, **kw: fake_spaces)
    monkeypatch.setattr("dailyfive.conductor.SunoClient", lambda *a, **kw: fake_suno)

    # Mirroring: write a plausible file instead of fetching one.
    def fake_download(url, dest, provider="x", **kw):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00" * 200_000)
        return 200_000
    monkeypatch.setattr("dailyfive.conductor.download", fake_download)
    monkeypatch.setattr("dailyfive.http.download", fake_download)

    # QC: real verdict logic, faked measurement (no ffmpeg in CI). The fake still
    # measures by slot type although the short lane is off: QC correctly rejects a
    # 192-second "short cut", so a slot-blind fake would silently fail the whole
    # lane the day SHORT_BRIEFS goes back above zero.
    def fake_measure(path):
        with session_scope() as s:
            clip = s.query(Clip).filter(Clip.local_path == str(path)).one_or_none()
            short = clip is not None and clip.slot_type.value == "short"
        return QCMetrics(duration_s=44.0 if short else 192.0, lufs_i=-11.4,
                         true_peak_db=-0.9, clip_samples=0, silence_ratio=0.02,
                         file_bytes=200_000, measured=True)
    monkeypatch.setattr(pl, "measure", fake_measure)

    monkeypatch.setattr(pl, "master_wav",
                        lambda src, dest, m: (Path(dest).write_bytes(b"WAV"), dest)[1])
    monkeypatch.setattr(pl, "encode_mp3",
                        lambda src, dest: (Path(dest).write_bytes(b"MP3"), dest)[1])
    monkeypatch.setattr(pl, "cover_art", lambda brief, dest, **kw: None)
    monkeypatch.setattr(pl, "tag_mp3", lambda *a, **kw: None)

    return {"suno": fake_suno, "spaces": fake_spaces}


def test_a_full_day_runs_end_to_end(wired):
    summary = pl.run_daily(date(2026, 8, 27))

    assert summary["clips"] == 14, "7 briefs x 2 clips each"
    assert summary["shipped"] == 5, "5 slots, all full-length"

    with session_scope() as s:
        run = s.query(Run).one()
        assert run.phase == RunPhase.SHIPPED
        assert run.error is None
        assert s.query(Signal).count() == 10
        assert s.query(Brief).count() == 7
        assert s.query(Job).count() == 7
        assert s.query(Decision).count() == 1

        cfg = settings()
        shipped = s.query(Clip).filter(Clip.shipped.is_(True)).all()
        assert len(shipped) == cfg.total_slots
        assert sum(1 for c in shipped if c.slot_type.value == "full") == cfg.full_slots
        assert sum(1 for c in shipped if c.slot_type.value == "short") == cfg.short_slots


def test_every_clip_is_recorded_not_just_the_shipped_ones(wired):
    """The rejections are what the studio learns from."""
    pl.run_daily(date(2026, 8, 27))
    with session_scope() as s:
        assert s.query(Clip).count() == 14
        unshipped = s.query(Clip).filter(Clip.shipped.is_(False)).all()
        assert len(unshipped) == 9
        assert all(c.qc_verdict is not None for c in unshipped)
        assert all(c.reject_reason for c in unshipped if c.qc_verdict == "pass")


def test_the_delivered_folder_has_everything(wired):
    pl.run_daily(date(2026, 8, 27))
    keys = wired["spaces"].objects
    assert "songs/2026-08-27/manifest.json" in keys
    assert "songs/2026-08-27/index.html" in keys
    assert "songs/2026-08-27/_rejected/rejects.json" in keys
    for name in ("master.wav", "master.mp3", "lyrics.txt", "lyrics.lrc", "meta.json"):
        assert sum(1 for k in keys if k.endswith(name)) == 5, f"missing {name}"


def test_the_day_page_carries_a_rating_control_per_song(wired):
    pl.run_daily(date(2026, 8, 27))
    page = wired["spaces"].objects["songs/2026-08-27/index.html"]
    import re
    assert len(re.findall(r"<button data-score=", page)) == 50   # 5 songs x 10
    assert "/ratings" in page


def test_shipped_metadata_reserves_the_distribution_block_empty(wired):
    import json
    pl.run_daily(date(2026, 8, 27))
    metas = [json.loads(v) for k, v in wired["spaces"].objects.items()
             if k.endswith("meta.json") and v.strip().startswith("{")]
    metas = metas or [json.loads(Path(v).read_text())
                      for k, v in wired["spaces"].objects.items()
                      if k.endswith("meta.json")]
    assert metas
    for m in metas:
        assert "distribution" in m
        assert all(v in (None, [], {}) for v in m["distribution"].values())


def test_rerunning_the_same_day_does_not_regenerate(wired):
    pl.run_daily(date(2026, 8, 27))
    tasks_after_first = wired["suno"].tasks
    pl.run_daily(date(2026, 8, 27))
    assert wired["suno"].tasks == tasks_after_first, \
        "a completed run must not re-submit and re-spend"


def test_a_crash_mid_run_resumes_without_paying_twice(wired, monkeypatch):
    """Kill the run after submission; restart; no second generation."""
    original = pl._phase_render

    def explode(run_id):
        raise RuntimeError("simulated crash")
    monkeypatch.setattr(pl, "_phase_render", explode)

    with pytest.raises(RuntimeError, match="simulated crash"):
        pl.run_daily(date(2026, 8, 27))
    tasks = wired["suno"].tasks
    assert tasks == 7

    monkeypatch.setattr(pl, "_phase_render", original)
    summary = pl.run_daily(date(2026, 8, 27))
    assert wired["suno"].tasks == tasks, "resume must not re-submit"
    assert summary["shipped"] == 5


def test_an_unconfigured_cover_model_is_stated_once_and_never_as_an_error(wired,
                                                                         monkeypatch):
    """Production logged five ERRORs a day for a key nobody intends to set.

    ARK_API_KEY is unset for the whole suite, so this is the deployed shape. The
    run must say so once, at WARNING, and record it where the console can read
    it — and no song may claim a cover.jpg that was never written.
    """
    import json

    from dailyfive import packager

    said: list[tuple[str, str]] = []
    monkeypatch.setattr(pl.log, "warning",
                        lambda msg, *a: said.append(("warning", msg % a if a else msg)))
    monkeypatch.setattr(pl.log, "error",
                        lambda msg, *a: said.append(("error", msg % a if a else msg)))

    def refuse(*a, **kw):
        raise AssertionError("cover_art must not be reached with no key configured")
    monkeypatch.setattr(pl, "cover_art", refuse)
    monkeypatch.setattr(packager, "cover_art", refuse)

    pl.run_daily(date(2026, 8, 27))

    art = [m for lvl, m in said if "cover art" in m]
    assert len(art) == 1, f"once per run, not once per song: {art}"
    assert "ARK_API_KEY unset" in art[0]
    assert not [m for lvl, m in said if lvl == "error" and "cover art" in m]

    with session_scope() as s:
        assert s.query(Run).one().notes["art"] == {"configured": False,
                                                   "reason": "ARK_API_KEY unset"}

    # FakeSpaces records the local path for an upload, not the bytes.
    metas = [json.loads(Path(v).read_text()) for k, v in wired["spaces"].objects.items()
             if k.endswith("meta.json")]
    assert len(metas) == 5
    assert all(m["files"]["cover"] is None for m in metas), \
        "a manifest must not name a file that is not in the folder"
    assert all("cover" in m["files"] for m in metas), "the key is reserved, not deleted"
    assert not [k for k in wired["spaces"].objects if k.endswith("cover.jpg")]
