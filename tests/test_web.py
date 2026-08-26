"""The one public surface: it must be hard to abuse and impossible to hang."""

import pytest
from fastapi.testclient import TestClient

from dailyfive.web.app import _phase_to_status, app
from dailyfive.web.templates import day_page


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health_is_answerable_with_an_empty_database(client):
    body = client.get("/health").json()
    assert body["ok"] is True and body["latest_run"] is None


def test_liveness_never_touches_the_database(client, monkeypatch):
    """The platform health check must not restart the one process that can

    explain a database outage. /healthz answers from the process alone, so a
    cluster that stops answering leaves the server up and /health readable.
    """
    import dailyfive.web.app as web

    def explode(*a, **kw):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(web, "session_scope", explode)
    assert client.get("/healthz").status_code == 200
    assert client.get("/health").status_code == 503


def test_wrong_webhook_secret_is_a_404_not_a_403(client):
    """A 403 confirms the path exists. A 404 tells a scanner nothing."""
    assert client.post("/webhooks/wrong/generate", json={}).status_code == 404


def test_callback_for_an_unknown_task_still_returns_200(client):
    """Suno abandons a callback after three failures — never give it one."""
    r = client.post("/webhooks/testsecret/generate",
                    json={"data": {"task_id": "nope"}})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_malformed_callback_body_still_returns_200(client):
    assert client.post("/webhooks/testsecret/generate", json={}).status_code == 200
    assert client.post("/webhooks/testsecret/wav", json={}).status_code == 200


def test_rating_rejects_an_out_of_range_score(client):
    assert client.post("/ratings", json={"clip_id": 1, "rating": 99}).status_code == 400
    assert client.post("/ratings", json={"clip_id": 1, "rating": 0}).status_code == 400


def test_rating_rejects_a_missing_field(client):
    assert client.post("/ratings", json={"rating": 5}).status_code == 400


def test_rating_a_nonexistent_clip_is_a_404(client):
    assert client.post("/ratings", json={"clip_id": 424242, "rating": 5}).status_code == 404


def test_callback_phase_names_map_to_polling_vocabulary():
    assert _phase_to_status("complete") == "SUCCESS"
    assert _phase_to_status("first") == "FIRST_SUCCESS"
    assert _phase_to_status("text") == "TEXT_SUCCESS"
    assert _phase_to_status("bogus") is None


def test_ratings_answer_a_cross_origin_preflight(client):
    """The day page is served from Spaces, so every rating is cross-origin.

    Without these headers the browser blocks the POST before it is sent and the
    loop never closes — with nothing in the server log to show for it.
    """
    r = client.options("/ratings", headers={
        "Origin": "https://bucket.nyc3.digitaloceanspaces.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") in ("*", "https://bucket.nyc3.digitaloceanspaces.com")
    assert "POST" in r.headers.get("access-control-allow-methods", "")


def test_cross_origin_post_carries_the_allow_header(client):
    r = client.post("/ratings", json={"clip_id": 1, "rating": 5},
                    headers={"Origin": "https://bucket.nyc3.digitaloceanspaces.com"})
    assert r.headers.get("access-control-allow-origin") is not None


def test_day_page_escapes_song_titles():
    from datetime import date
    html = day_page(date(2026, 8, 27),
                    [{"clip_id": 1, "title": "<script>alert(1)</script>",
                      "mp3_url": "https://x/a.mp3"}],
                    api_base="https://songs.test")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_day_page_has_a_full_rating_scale_per_song():
    import re
    from datetime import date
    html = day_page(date(2026, 8, 27),
                    [{"clip_id": i, "title": f"S{i}", "mp3_url": "u"} for i in (1, 2)],
                    api_base="https://songs.test")
    assert len(re.findall(r"<button data-score=", html)) == 20
    assert '"https://songs.test"' in html


def test_day_page_survives_a_song_with_no_audio():
    from datetime import date
    html = day_page(date(2026, 8, 27), [{"clip_id": 1, "title": "S"}],
                    api_base="https://songs.test")
    assert "audio unavailable" in html


def test_existing_ratings_endpoint_returns_only_what_was_asked_for(client):
    """The day page hydrates from this; without it a rating made on one device
    is invisible on another and gets recorded twice."""
    assert client.get("/ratings?clip_ids=").json() == {"ratings": {}}
    assert client.get("/ratings?clip_ids=1,2,3").json() == {"ratings": {}}


def test_existing_ratings_ignores_junk_ids(client):
    r = client.get("/ratings?clip_ids=abc,,7,%20,-1")
    assert r.status_code == 200 and r.json() == {"ratings": {}}


def test_day_page_preloads_metadata_so_the_duration_shows():
    from datetime import date
    html = day_page(date(2026, 8, 27),
                    [{"clip_id": 1, "title": "S", "mp3_url": "u"}],
                    api_base="https://songs.test")
    assert 'preload="metadata"' in html
    assert 'preload="none"' not in html


def test_day_page_hydrates_ratings_from_the_server():
    from datetime import date
    html = day_page(date(2026, 8, 27),
                    [{"clip_id": 1, "title": "S", "mp3_url": "u"}],
                    api_base="https://songs.test")
    assert "/ratings?clip_ids=" in html


def test_day_page_themes_the_native_audio_controls():
    """Browser-drawn <audio> controls ignore the stylesheet; only color-scheme
    stops them rendering light against a dark page."""
    from datetime import date
    html = day_page(date(2026, 8, 27), [{"clip_id": 1, "title": "S", "mp3_url": "u"}],
                    api_base="https://songs.test")
    assert "color-scheme" in html


def test_rating_scale_is_a_grid_not_a_wrapping_row():
    """flex-wrap leaves a ragged 8 + 2 with two stretched buttons on a phone."""
    from datetime import date
    html = day_page(date(2026, 8, 27), [{"clip_id": 1, "title": "S", "mp3_url": "u"}],
                    api_base="https://songs.test")
    assert "grid-template-columns:repeat(10,1fr)" in html
    assert "grid-template-columns:repeat(5,1fr)" in html
