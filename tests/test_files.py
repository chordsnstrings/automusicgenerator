"""The byte path an <audio> element depends on.

Two things are being pinned here. Range handling, because a player that cannot
seek is a player with a dead scrub bar. And peak memory, because this endpoint
reads a column that holds tens of megabytes of audio on a 512 MB instance —
materialising it whole is an outage, not a slow page.
"""

import tracemalloc

import pytest
from fastapi.testclient import TestClient

from dailyfive.config import reload_settings
from dailyfive.db import session_scope
from dailyfive.models import StoredFile, utcnow
from dailyfive.storage import open_store
from dailyfive.web.app import app

BODY = bytes(range(256)) * 8          # 2048 bytes, and every byte distinguishable


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AUDIO_STORE", "database")
    reload_settings()
    return open_store()


def _seed(store, key: str, data: bytes, content_type: str) -> str:
    """put_text builds a well-formed row; only audio is not text, so the bytes
    go in afterwards with the digest and length kept in step with them."""
    import hashlib
    store.put_text("", key, content_type=content_type)
    with session_scope() as s:
        row = s.query(StoredFile).filter(StoredFile.key == key).one()
        row.data, row.size_bytes = data, len(data)
        row.sha256 = hashlib.sha256(data).hexdigest()
    return key


@pytest.fixture
def key(store):
    return _seed(store, store.key_for("2026-08-27", "01_slow-burn", "master.mp3"),
                 BODY, "audio/mpeg")


RANGES = [
    ("bytes=0-1023", 206, "bytes 0-1023/2048", 1024),
    ("bytes=0-", 206, "bytes 0-2047/2048", 2048),
    ("bytes=100-", 206, "bytes 100-2047/2048", 1948),
    ("bytes=-500", 206, "bytes 1548-2047/2048", 500),
    ("bytes=-0", 416, "bytes */2048", None),
    ("bytes=99999999999-", 416, "bytes */2048", None),
    ("bytes=100-50", 416, "bytes */2048", None),
    ("bytes=0-99999999999999", 206, "bytes 0-2047/2048", 2048),
    ("bytes=0-99,200-299", 206, "bytes 0-99/2048", 100),
    ("bytes=abc-def", 200, None, 2048),
    ("items=0-10", 200, None, 2048),
]


@pytest.mark.parametrize("header,status,content_range,length", RANGES)
def test_range_requests_are_answered_as_asked(client, key, header, status,
                                              content_range, length):
    r = client.get(f"/files/{key}", headers={"Range": header})
    assert r.status_code == status, header
    assert r.headers.get("content-range") == content_range, header
    if length is not None:
        assert len(r.content) == length, header


def test_a_malformed_range_is_ignored_rather_than_refused(client, key):
    """RFC 9110 §14.2. A 416 would fail a request the client could survive."""
    r = client.get(f"/files/{key}", headers={"Range": "bytes=abc-def"})
    assert r.status_code == 200 and r.content == BODY


def test_ranges_reassemble_into_the_original_bytes(client, key):
    parts = []
    for start in range(0, len(BODY), 500):
        r = client.get(f"/files/{key}",
                       headers={"Range": f"bytes={start}-{start + 499}"})
        assert r.status_code == 206
        parts.append(r.content)
    assert b"".join(parts) == BODY


def test_a_whole_file_request_is_still_a_200(client, key):
    """A download link issues a rangeless GET; answering 206 to it is legal but
    download managers differ on how they continue one."""
    r = client.get(f"/files/{key}")
    assert r.status_code == 200
    assert r.content == BODY
    assert r.headers["content-length"] == str(len(BODY))
    assert r.headers["accept-ranges"] == "bytes"


def test_a_matching_etag_costs_nothing(client, key):
    etag = client.get(f"/files/{key}").headers["etag"]
    r = client.get(f"/files/{key}", headers={"If-None-Match": etag})
    assert r.status_code == 304 and r.content == b""


def test_the_content_type_is_never_sniffed(client, key):
    """This origin serves LLM-authored text/html out of the same table."""
    assert client.get(f"/files/{key}").headers["x-content-type-options"] == "nosniff"


def test_an_empty_file_refuses_a_range_honestly(client, store):
    k = store.key_for("2026-08-27", "01_slow-burn", "lyrics.txt")
    store.put_text("", k, content_type="text/plain")
    r = client.get(f"/files/{k}", headers={"Range": "bytes=0-100"})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */0"


def test_an_expired_row_is_gone_as_far_as_the_reader_is_concerned(client, key):
    """The retention window is a fact about the row, so the read path has to
    honour it — otherwise a file lives on until the purge next runs."""
    from datetime import timedelta
    with session_scope() as s:
        s.query(StoredFile).filter(StoredFile.key == key).one().expires_at = \
            utcnow() - timedelta(days=1)
    assert client.get(f"/files/{key}").status_code == 404


def test_a_missing_key_is_a_404(client, store):
    assert client.get("/files/songs/2026-08-27/nope/master.mp3").status_code == 404


def test_serving_a_slice_does_not_materialise_the_column(client, store):
    """The assertion that stops the whole-column read coming back.

    32 MB in the row, one byte asked for. Reading `data` whole would show up
    here as a 32 MB peak (worse on Postgres, where bytea arrives hex-encoded);
    the 8 MB ceiling leaves a wide margin and still cannot be passed by a
    version of this endpoint that loads the file to slice it in Python.
    """
    k = _seed(store, store.key_for("2026-08-27", "01_slow-burn", "master.wav"),
              b"\0" * (32 << 20), "audio/wav")

    tracemalloc.start()
    try:
        r = client.get(f"/files/{k}", headers={"Range": "bytes=0-0"})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert r.status_code == 206 and len(r.content) == 1
    assert peak < 8 << 20, f"peaked at {peak / 1e6:.1f} MB serving one byte"


def test_head_answers_with_the_length_and_no_body(client, key):
    """The probe every link-checker and most media clients send first.

    FastAPI's APIRoute, unlike Starlette's Route, does not fold HEAD in beside
    GET, so this route answered 405 to the one request a proxy makes before it
    streams anything.
    """
    r = client.head(f"/files/{key}")
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(BODY))
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == b""


def test_head_ignores_range_and_reports_the_whole_representation(client, key):
    """RFC 9110 §14.2 defines range handling for GET only, so a ranged HEAD is
    still asking how big the file is. Answering 206 with a slice length would
    misstate it."""
    r = client.head(f"/files/{key}", headers={"Range": "bytes=0-99"})
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(BODY))
    assert "content-range" not in r.headers


def test_head_of_a_missing_key_is_a_404(client, store):
    assert client.head("/files/songs/2026-08-27/nope/master.mp3").status_code == 404


def test_a_conditional_head_still_takes_the_304(client, key):
    """The short-circuit sits after the validator check, so a HEAD that already
    holds the bytes gets the same free answer a GET does."""
    etag = client.head(f"/files/{key}").headers["etag"]
    assert client.head(f"/files/{key}", headers={"If-None-Match": etag}).status_code == 304


def test_head_does_not_read_the_bytes_it_reports_the_length_of(client, store, monkeypatch):
    """The only test that fails under the obvious fix, and so the only thing
    guarding it.

    Adding HEAD to the route's methods and letting the request fall through to
    the StreamingResponse passes every assertion above: Starlette runs the
    generator to exhaustion and uvicorn discards the chunks a layer later, so
    status and Content-Length look identical either way while 32 MB moves
    through a 512 MB container per probe. Counting the generator is what makes
    that difference visible, and the peak ceiling is borrowed from
    test_serving_a_slice_does_not_materialise_the_column for the same reason.
    """
    from dailyfive.web import app as appmod

    k = _seed(store, store.key_for("2026-08-27", "01_slow-burn", "master.wav"),
              b"\0" * (32 << 20), "audio/wav")

    calls: list[tuple] = []
    real = appmod._chunks
    monkeypatch.setattr(appmod, "_chunks",
                        lambda *a, **kw: (calls.append(a), real(*a, **kw))[1])

    tracemalloc.start()
    try:
        r = client.head(f"/files/{k}")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert r.status_code == 200
    assert r.headers["content-length"] == str(32 << 20)
    assert r.content == b""
    assert calls == [], "HEAD streamed the file it was only asked the size of"
    assert peak < 8 << 20, f"peaked at {peak / 1e6:.1f} MB answering a HEAD"
