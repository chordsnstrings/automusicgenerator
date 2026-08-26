import hashlib, tracemalloc
import pytest
from fastapi.testclient import TestClient

from dailyfive.config import reload_settings, settings
from dailyfive.db import session_scope
from dailyfive.models import StoredFile, utcnow
from dailyfive.storage import open_store
from dailyfive.web.app import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AUDIO_STORE", "database")
    reload_settings()
    return open_store()


def _seed(store, key, data, ct):
    store.put_text("", key, content_type=ct)
    with session_scope() as s:
        row = s.query(StoredFile).filter(StoredFile.key == key).one()
        row.data, row.size_bytes = data, len(data)
        row.sha256 = hashlib.sha256(data).hexdigest()
    return key


def test_head_vs_get_length_agreement(client, store):
    body = b"x" * 5000
    k = _seed(store, "songs/2026-08-27/01_a/master.mp3", body, "audio/mpeg")
    h = client.head(f"/files/{k}")
    g = client.get(f"/files/{k}")
    print("HEAD", h.status_code, dict(h.headers))
    print("GET ", g.status_code, g.headers.get("content-length"), len(g.content))
    assert h.headers["content-length"] == g.headers["content-length"]


def test_head_on_zero_byte_row(client, store):
    k = store.put_text("", "songs/2026-08-27/01_a/lyrics.txt",
                       content_type="text/plain; charset=utf-8")
    h = client.head(f"/files/{k}")
    g = client.get(f"/files/{k}")
    print("zero HEAD", h.status_code, dict(h.headers))
    print("zero GET", g.status_code, g.headers.get("content-length"), repr(g.content))


def test_head_on_expired_row(client, store):
    from datetime import timedelta
    k = _seed(store, "songs/2026-08-27/01_a/master.wav", b"y" * 10, "audio/wav")
    with session_scope() as s:
        s.query(StoredFile).filter(StoredFile.key == k).one().expires_at = utcnow() - timedelta(days=1)
    print("expired head", client.head(f"/files/{k}").status_code)


def test_retype_idempotent_and_data_untouched(store):
    from dailyfive.storage import retype_stored_files
    body = b"z" * 100
    keys = {
        "songs/d/01_a/master.mp3": ("audio/mpeg", body),
        "songs/d/01_a/master.wav": ("audio/x-wav", body),
        "songs/d/01_a/lyrics.lrc": ("application/octet-stream", b"[00:01.00]hi"),
        "songs/d/01_a/lyrics.txt": ("text/plain", b"hi"),
        "songs/d/01_a/meta.json": ("application/json", b"{}"),
        "songs/d/index.html": ("text/html; charset=utf-8", b"<p>"),
        "backups/dailyfive-2026.sql.gz": ("application/sql", b"\x1f\x8b"),
        "songs/d/01_a/cover.jpg": ("image/jpeg", b"\xff\xd8"),
        "songs/d/weird": ("application/octet-stream", b"?"),
    }
    for k, (ct, data) in keys.items():
        _seed(store, k, data, ct)
    before = {}
    with session_scope() as s:
        for r in s.query(StoredFile).all():
            before[r.key] = (bytes(r.data), r.sha256, r.size_bytes, r.kind)
    r1 = retype_stored_files()
    print("pass1", r1)
    r2 = retype_stored_files()
    print("pass2", r2)
    assert r2["fixed"] == 0, "not idempotent"
    with session_scope() as s:
        for r in s.query(StoredFile).all():
            assert before[r.key] == (bytes(r.data), r.sha256, r.size_bytes, r.kind), r.key
            print(" ", r.key, "->", r.content_type, "kind", r.kind)


def test_retype_empty_db():
    from dailyfive.storage import retype_stored_files
    print("empty:", retype_stored_files(), retype_stored_files(dry_run=True))


def test_unrate_empty_db(client):
    from dailyfive.archivist import unrate, learning_status
    print("unrate missing:", unrate(999))
    print("status:", learning_status())
    print("delete missing clip:", client.delete("/ratings/999").status_code)


def test_shape_line_variants(monkeypatch):
    from dailyfive.web import views
    for full, short in ((5, 0), (3, 2), (0, 2), (0, 0)):
        monkeypatch.setenv("FULL_SLOTS", str(full))
        monkeypatch.setenv("SHORT_SLOTS", str(short))
        monkeypatch.setenv("FULL_BRIEFS", str(full + 2))
        monkeypatch.setenv("SHORT_BRIEFS", str(short))
        reload_settings()
        print((full, short), "->", views._shape_line(settings()))
