"""Retention: the window is a fact on the row, and the purge reads only that."""

from datetime import timedelta

import pytest

from dailyfive.config import reload_settings
from dailyfive.db import session_scope
from dailyfive.models import StoredFile, utcnow
from dailyfive.retention import due, purge, usage
from dailyfive.storage import DatabaseStore, open_store


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AUDIO_STORE", "database")
    monkeypatch.setenv("RETENTION_DAYS", "30")
    reload_settings()
    return open_store()


def test_database_store_is_chosen_by_config(store):
    assert isinstance(store, DatabaseStore)
    assert store.retention_days == 30


def test_every_write_carries_its_own_expiry(store):
    key = store.key_for("2026-08-26", "01_song", "master.mp3")
    store.put_text("audio-bytes", key, content_type="audio/mpeg")
    with session_scope() as s:
        row = s.query(StoredFile).filter(StoredFile.key == key).one()
        assert row.expires_at is not None
        days = (row.expires_at - row.created_at).days
        assert 29 <= days <= 30
        assert row.kind == "mp3" and row.sha256


def test_purge_deletes_only_what_has_expired(store):
    live = store.key_for("2026-08-26", "01", "master.mp3")
    dead = store.key_for("2026-06-01", "01", "master.wav")
    store.put_text("a", live, content_type="audio/mpeg")
    store.put_text("b", dead, content_type="audio/wav")
    with session_scope() as s:
        s.query(StoredFile).filter(StoredFile.key == dead).one().expires_at = \
            utcnow() - timedelta(days=1)

    assert due() == 1
    result = purge()
    assert result["removed"] == 1 and result["by_kind"] == {"wav": 1}
    assert store.exists(live) and not store.exists(dead)


def test_dry_run_changes_nothing(store):
    key = store.key_for("2026-06-01", "01", "master.wav")
    store.put_text("b", key, content_type="audio/wav")
    with session_scope() as s:
        s.query(StoredFile).filter(StoredFile.key == key).one().expires_at = \
            utcnow() - timedelta(days=1)
    assert purge(dry_run=True)["removed"] == 1
    assert store.exists(key), "a dry run must not delete"
    assert due() == 1


def test_retention_window_is_configurable(monkeypatch):
    monkeypatch.setenv("AUDIO_STORE", "database")
    monkeypatch.setenv("RETENTION_DAYS", "7")
    reload_settings()
    st = open_store()
    key = st.key_for("2026-08-26", "01", "master.mp3")
    st.put_text("x", key, content_type="audio/mpeg")
    with session_scope() as s:
        row = s.query(StoredFile).filter(StoredFile.key == key).one()
        assert 6 <= (row.expires_at - row.created_at).days <= 7


def test_rewriting_a_key_refreshes_the_window_and_does_not_duplicate(store):
    key = store.key_for("2026-08-26", "01", "master.mp3")
    store.put_text("first", key, content_type="audio/mpeg")
    store.put_text("second", key, content_type="audio/mpeg")
    with session_scope() as s:
        rows = s.query(StoredFile).filter(StoredFile.key == key).all()
        assert len(rows) == 1 and rows[0].data == b"second"


def test_usage_projects_where_the_store_settles(store):
    u = usage()
    assert u["retention_days"] == 30
    # 5 slots x 30 days x ~55 MB
    assert 7.0 <= u["projected_steady_gb"] <= 9.5


def test_purge_on_an_empty_store_is_a_no_op(store):
    assert purge()["removed"] == 0


def test_headroom_is_unknown_until_the_disk_is_declared(monkeypatch):
    """Undeclared reads as unknown, never as enough. Guessing a managed
    cluster's disk from its plan name is exactly the plausible-looking
    assumption this codebase refuses everywhere else, and guessing high ends in
    a read-only database with the only copy of the audio inside it."""
    from dailyfive.config import reload_settings
    from dailyfive.retention import usage

    monkeypatch.setenv("AUDIO_STORE", "database")
    monkeypatch.setenv("DB_DISK_GB", "")
    reload_settings()
    u = usage()
    assert u["disk_gb"] is None
    assert u["headroom_gb"] is None
    assert u["projected_steady_gb"] > 0, "a projection with nothing to fail against"


def test_the_retention_window_is_weighed_against_a_declared_disk(monkeypatch):
    """RETENTION_DAYS decides where the store settles, and the figure was
    doubled from 4.1 GB to 8.2 with no capacity anywhere to compare it to."""
    from dailyfive.config import reload_settings
    from dailyfive.retention import usage

    monkeypatch.setenv("AUDIO_STORE", "database")
    monkeypatch.setenv("DB_DISK_GB", "10")
    cfg = reload_settings()
    u = usage()
    assert u["disk_gb"] == 10
    assert u["headroom_gb"] == round(10 - u["projected_steady_gb"], 1)

    monkeypatch.setenv("RETENTION_DAYS", str(cfg.retention_days * 4))
    reload_settings()
    assert usage()["headroom_gb"] < 0, "a window that does not fit reads as fitting"
