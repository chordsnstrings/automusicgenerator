"""Every test runs against an in-memory database and never touches the network."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PUBLIC_BASE_URL", "https://songs.test")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("SUNO_MODEL", "V5_5")


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    """A private database per test, so ordering never matters."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    # No test should ever wait on a real polling interval.
    monkeypatch.setenv("POLL_INTERVAL_S", "0")
    monkeypatch.setenv("GENERATION_TIMEOUT_S", "5")
    monkeypatch.setenv("WAV_TIMEOUT_S", "5")
    from dailyfive import db
    from dailyfive.config import reload_settings
    db.reset_engine()
    reload_settings()
    db.init_db()
    yield
    db.reset_engine()


@pytest.fixture
def run_id():
    from datetime import date
    from dailyfive.db import session_scope
    from dailyfive.models import Run, RunPhase
    with session_scope() as s:
        r = Run(run_date=date(2026, 8, 27), phase=RunPhase.CREATED)
        s.add(r)
        s.flush()
        return r.id


@pytest.fixture
def brief_factory(run_id):
    from dailyfive.db import session_scope
    from dailyfive.models import Brief, SlotType

    def make(idx=0, slot="full", **kw):
        with session_scope() as s:
            b = Brief(
                run_id=run_id, slot_type=SlotType(slot), idx=idx,
                title=kw.get("title", f"Song {idx}"),
                theme=kw.get("theme", "a specific situation"),
                bpm=kw.get("bpm", 84), musical_key=kw.get("key", "F minor"),
                style_string=kw.get("style_string", "dark rnb, 808s, close-mic vocal"),
                lyrics=kw.get("lyrics", "[Verse]\nline one\n[Chorus]\nthe hook"),
                lyric_hash=kw.get("lyric_hash", f"hash{idx}"),
                vocal_gender=kw.get("vocal_gender", "f"),
                negative_tags=kw.get("negative_tags", "lo-fi, muddy mix"),
            )
            s.add(b)
            s.flush()
            return b.id
    return make
