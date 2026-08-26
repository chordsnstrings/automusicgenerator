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
