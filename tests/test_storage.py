"""What an artifact is announced as, and how a row already announced wrongly
gets corrected.

The content type is stamped at write time, so this is not a property of the
response path: a wrong value reaches S3 as ContentType, survives a backup
restore, and is what the browser is told forever. And the derivation it replaces
read /etc/mime.types, which makes the answer depend on the base image rather
than on a decision.
"""

import pytest
from fastapi.testclient import TestClient

from dailyfive.config import reload_settings
from dailyfive.db import session_scope
from dailyfive.models import StoredFile
from dailyfive.storage import (content_type_for, open_store,
                               remeta_stored_files, retype_stored_files)
from dailyfive.web.app import app

# The basenames the ship loop writes into every delivered folder (pipeline.py).
SHIPPED = ("master.wav", "master.mp3", "cover.jpg",
           "lyrics.txt", "lyrics.lrc", "meta.json")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AUDIO_STORE", "database")
    reload_settings()
    return open_store()


def test_an_uploaded_lrc_is_served_as_readable_text(client, store, tmp_path):
    """The gap as it was found: a browser downloaded the lyrics instead of
    showing them, because guess_type has no .lrc entry and the octet-stream
    fallback fired — and nosniff, which this origin has to send, removes the
    browser's only way to recover from that."""
    local = tmp_path / "lyrics.lrc"
    local.write_text("[00:12.00] a line with an em-dash — and an accent é\n",
                     encoding="utf-8")
    key = store.upload(local, store.key_for("2026-08-27", "01_slow-burn", "lyrics.lrc"))

    r = client.get(f"/files/{key}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/plain; charset=utf-8"


def test_no_shipped_basename_is_ever_stored_as_octet_stream(store, tmp_path):
    """The test that would have caught this before it reached production.

    Every name the ship loop writes has to resolve to something a client can act
    on. octet-stream is the fallback that means "we did not decide", and on any
    of these six it is a bug rather than an answer.
    """
    for name in SHIPPED:
        local = tmp_path / name
        local.write_bytes(b"x")
        key = store.upload(local, store.key_for("2026-08-27", "01_slow-burn", name))
        with session_scope() as s:
            stored = s.query(StoredFile).filter(StoredFile.key == key).one().content_type
        assert stored != "application/octet-stream", name
        assert stored == content_type_for(name)


def test_the_wav_type_does_not_move_with_the_base_image(tmp_path):
    """guess_type answers audio/x-wav where /etc/mime.types is installed and
    something else where it is not, and CPython moved the built-in mapping in
    3.13 — so an unpinned .wav changes what production serves on a routine
    base-image bump, with no code change to review."""
    assert content_type_for("master.wav") == "audio/wav"


def test_a_backup_is_announced_as_the_container_it_is():
    """guess_type answers ('application/sql', 'gzip') for a .sql.gz and both
    upload paths take [0], so the bytes of a gzip stream would be announced as
    SQL text."""
    assert content_type_for("dailyfive-20260826T030000Z.sql.gz") == "application/gzip"


def test_an_unlisted_extension_still_falls_back_to_guess_type():
    assert content_type_for("notes.md").startswith("text/")


def test_retype_corrects_a_row_written_before_the_fix_and_then_does_nothing(store):
    """The repair has to be re-runnable, which is why it is a command and not a
    data migration: a migration fires once and cannot catch a row written by an
    old container mid-deploy or restored from a pre-fix backup."""
    key = store.put_text("[00:01.00] line", store.key_for("2026-08-27", "01_x", "lyrics.lrc"),
                         content_type="application/octet-stream")

    first = retype_stored_files()
    assert first["fixed"] == 1
    assert first["by_type"] == {"text/plain; charset=utf-8": 1}
    with session_scope() as s:
        assert s.query(StoredFile).filter(StoredFile.key == key).one().content_type == \
            "text/plain; charset=utf-8"

    assert retype_stored_files()["fixed"] == 0


def test_a_retype_dry_run_reports_without_writing(store):
    key = store.put_text("x", store.key_for("2026-08-27", "01_x", "lyrics.lrc"),
                         content_type="application/octet-stream")
    assert retype_stored_files(dry_run=True)["fixed"] == 1
    with session_scope() as s:
        assert s.query(StoredFile).filter(StoredFile.key == key).one().content_type == \
            "application/octet-stream"
    assert retype_stored_files(dry_run=True)["fixed"] == 1, "a dry run must change nothing"


def test_retype_does_not_read_the_bytes_of_every_row(store):
    """It walks the whole table, and the table holds masters. Selecting the row
    rather than its columns would pull tens of megabytes each."""
    import tracemalloc

    key = store.put_text("", store.key_for("2026-08-27", "01_x", "master.wav"),
                         content_type="application/octet-stream")
    with session_scope() as s:
        row = s.query(StoredFile).filter(StoredFile.key == key).one()
        row.data, row.size_bytes = b"\0" * (32 << 20), 32 << 20

    tracemalloc.start()
    try:
        result = retype_stored_files()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result["fixed"] == 1
    assert peak < 8 << 20, f"peaked at {peak / 1e6:.1f} MB retyping one row"


def _meta(cover):
    import json
    return json.dumps({"title": "T", "files": {
        "wav": "master.wav", "mp3": "master.mp3", "cover": cover,
        "lyrics_txt": "lyrics.txt", "lyrics_lrc": "lyrics.lrc"}}, indent=2)


def _stored_meta(key):
    import json
    with session_scope() as s:
        return json.loads(bytes(
            s.query(StoredFile).filter(StoredFile.key == key).one().data))


def test_remeta_drops_a_cover_that_was_never_made_and_then_does_nothing(store):
    """Every manifest shipped before cover art became conditional names
    cover.jpg, and no such file has ever existed. Fixing build_meta fixes
    tomorrow's; these stay false for the rest of their retention window."""
    key = store.put_text(_meta("cover.jpg"),
                         store.key_for("2026-08-27", "01_x", "meta.json"))

    assert remeta_stored_files()["fixed"] == 1
    doc = _stored_meta(key)
    assert doc["files"]["cover"] is None
    assert doc["files"]["mp3"] == "master.mp3", "only the cover entry is touched"

    assert remeta_stored_files()["fixed"] == 0


def test_remeta_leaves_a_manifest_whose_cover_exists(store, tmp_path):
    local = tmp_path / "cover.jpg"
    local.write_bytes(b"jpeg")
    store.upload(local, store.key_for("2026-08-27", "01_x", "cover.jpg"))
    key = store.put_text(_meta("cover.jpg"),
                         store.key_for("2026-08-27", "01_x", "meta.json"))

    assert remeta_stored_files()["fixed"] == 0
    assert _stored_meta(key)["files"]["cover"] == "cover.jpg"


def test_remeta_does_not_read_a_purge_as_a_manifest_to_correct(store):
    """Every folder loses its bytes on the same clock, so after a purge the
    audio is gone too. Blanking those names would turn retention into a record
    that the day never had a master."""
    key = store.put_text(_meta(None),
                         store.key_for("2026-08-01", "01_x", "meta.json"))
    assert remeta_stored_files()["fixed"] == 0
    assert _stored_meta(key)["files"]["wav"] == "master.wav"


def test_repairing_a_manifest_does_not_extend_how_long_the_day_is_kept(store):
    """put_text stamps a fresh expires_at. A sentence about a day is not a
    reason to keep that day's bytes longer."""
    key = store.put_text(_meta("cover.jpg"),
                         store.key_for("2026-08-27", "01_x", "meta.json"))
    with session_scope() as s:
        before = s.query(StoredFile).filter(StoredFile.key == key).one().expires_at

    assert remeta_stored_files()["fixed"] == 1
    with session_scope() as s:
        row = s.query(StoredFile).filter(StoredFile.key == key).one()
    assert row.expires_at == before
    assert row.size_bytes == len(bytes(row.data)), "size must follow the rewrite"


def test_a_remeta_dry_run_reports_without_writing(store):
    key = store.put_text(_meta("cover.jpg"),
                         store.key_for("2026-08-27", "01_x", "meta.json"))
    assert remeta_stored_files(dry_run=True)["fixed"] == 1
    assert _stored_meta(key)["files"]["cover"] == "cover.jpg"
    assert remeta_stored_files(dry_run=True)["fixed"] == 1


def test_remeta_leaves_a_manifest_it_cannot_parse(store, caplog):
    """A row it could not read is not a row to overwrite."""
    key = store.put_text("{not json",
                         store.key_for("2026-08-27", "01_x", "meta.json"))
    assert remeta_stored_files()["fixed"] == 0
    with session_scope() as s:
        assert bytes(s.query(StoredFile).filter(
            StoredFile.key == key).one().data) == b"{not json"
