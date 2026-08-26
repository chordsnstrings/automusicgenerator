"""The console must render from any database state, including a broken config.

A dashboard that 500s when something is wrong is useless exactly when it is
needed, so every page is exercised against an empty database as well as a
populated one.
"""

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from dailyfive.config import reload_settings
from dailyfive.db import session_scope
from dailyfive.models import (AgentCall, Brief, Clip, Decision, Job, JobState,
                              Outcome, Run, RunPhase, Signal, SlotType)
from dailyfive.storage import open_store
from dailyfive.web.app import app

PAGES = ["/", "/runs", "/agents", "/codex", "/files"]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def populated(run_id, brief_factory):
    bid = brief_factory(title="Slow Burn in June")
    with session_scope() as s:
        s.add(Signal(run_id=run_id, rank=1, theme="a specific situation",
                     sentiment="wistful", sources=["gtrends"], lead="leading",
                     confidence=0.72, evidence="rising search interest"))
        j = Job(run_id=run_id, brief_id=bid, idempotency_key="k", task_id="t1",
                state=JobState.MIRRORED, attempts=1, callbacks_seen=2, polls=1,
                payload={})
        s.add(j); s.flush()
        for v, shipped, verdict, reason in ((0, True, "pass", None),
                                            (1, False, "fail", "true peak clipping")):
            s.add(Clip(run_id=run_id, job_id=j.id, brief_id=bid,
                       audio_id=f"a{v}", variant=v, slot_type=SlotType.FULL,
                       title="Slow Burn in June", duration_s=196.0,
                       qc={"lufs_i": -11.4, "true_peak_db": -0.9,
                           "silence_ratio": 0.02},
                       qc_verdict=verdict, qc_reason=reason, shipped=shipped,
                       rank=1 if shipped else None, score_total=8.1 if shipped else 6.2,
                       score_hook=8.0, score_mix=7.5, score_trend=9.0,
                       spaces_key="songs/2026-08-27/01_slow-burn"))
        s.add(AgentCall(run_id=run_id, role="scout", label="signal-sheet",
                        provider="minimax", model="MiniMax-M3", ok=True, ms=12900,
                        chars_in=41000, chars_out=2790))
        s.add(AgentCall(run_id=run_id, role="producer", label="score:hook",
                        provider="minimax", model="MiniMax-M3", ok=False, ms=400,
                        chars_in=900, chars_out=0, error="rate limited"))
        s.add(Decision(run_id=run_id, rationale="a coherent day", picks=[], rejections=[]))
        s.get(Run, run_id).phase = RunPhase.SHIPPED
    return run_id


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_on_an_empty_database(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "<html" in r.text and "The Daily Five" in r.text


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_with_data(client, populated, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", PAGES)
def test_every_page_answers_head(client, path):
    """FastAPI does not fold HEAD in beside GET the way Starlette does, so
    every console page answered 405 to a link-checker."""
    assert client.head(path).status_code == 200


def test_every_page_renders_with_an_unusable_brain_config(client, monkeypatch):
    """A misconfigured provider must show as a problem, not crash the console."""
    from dailyfive.config import reload_settings
    monkeypatch.setenv("LLM_DEFAULT", "not-a-real-provider")
    reload_settings()
    for path in PAGES:
        r = client.get(path)
        assert r.status_code == 200, path
    assert "misconfigured" in client.get("/").text


def test_run_detail_shows_the_cut_candidates_and_why(client, populated):
    r = client.get("/runs/2026-08-27")
    assert r.status_code == 200
    body = r.text
    assert "Slow Burn in June" in body
    assert "QC cut" in body
    assert "true peak clipping" in body, "the reason a clip was cut must be visible"
    assert "Every candidate" in body


def test_run_detail_shows_which_brain_answered(client, populated):
    body = client.get("/runs/2026-08-27").text
    assert "minimax" in body and "MiniMax-M3" in body
    assert "signal-sheet" in body
    assert "rate limited" in body, "a failed brain call must be visible"


def test_run_detail_shows_the_scout_evidence(client, populated):
    body = client.get("/runs/2026-08-27").text
    assert "rising search interest" in body
    assert "gtrends" in body


def test_missing_run_is_a_404_not_a_crash(client):
    assert client.get("/runs/2020-01-01").status_code == 404


def test_malformed_date_is_a_400(client):
    assert client.get("/runs/not-a-date").status_code == 400


def test_agents_page_names_the_roles_without_a_brain(client):
    body = client.get("/agents").text
    for name in ("Conductor", "QC Engineer", "Packager", "Prompt Compiler"):
        assert name in body
    assert "no LLM" in body


def test_codex_page_shows_persona_registration_state(client):
    body = client.get("/codex").text
    assert "Persona cast" in body
    assert "not created" in body, "an unregistered persona must be visible as a gap"


def test_console_escapes_titles_from_the_database(client, run_id, brief_factory):
    bid = brief_factory(title="<script>alert(1)</script>")
    with session_scope() as s:
        s.get(Run, run_id).phase = RunPhase.SHIPPED
    body = client.get("/runs/2026-08-27").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# ── listening ────────────────────────────────────────────────────────────────
# conftest neutralises AUDIO_STORE, so open_store() falls through to LocalStore
# and no stored_files row exists anywhere by default. Every test above therefore
# already exercises the "the bytes are not here" path; these opt in.
@pytest.fixture
def delivered(populated, monkeypatch):
    """The files the ship loop would have written for the shipped clip."""
    monkeypatch.setenv("AUDIO_STORE", "database")
    reload_settings()
    store = open_store()
    with session_scope() as s:
        clip = s.query(Clip).filter(Clip.shipped.is_(True)).one()
        clip_id, run_id, folder = clip.id, clip.run_id, clip.spaces_key
    for name, ctype in (("master.mp3", "audio/mpeg"), ("master.wav", "audio/wav"),
                        ("lyrics.txt", "text/plain")):
        store.put_text("id3", f"{folder}/{name}", content_type=ctype,
                       clip_id=clip_id, run_id=run_id)
    return store


def test_the_files_page_plays_every_shipped_song(client, delivered):
    """The console is where you go to listen. A page that lists songs with no
    way to play them is the bug this pins."""
    body = client.get("/files").text
    assert 'src="/files/songs/2026-08-27/01_slow-burn/master.mp3"' in body
    assert len(re.findall("<audio controls", body)) == 1, "one player per shipped song"
    assert 'href="/files/songs/2026-08-27/01_slow-burn/master.wav" download' in body
    assert "wav · mp3 · cover · lyrics · lrc · meta" not in body, \
        "the old dead text advertised files it never linked to"


def test_a_shipped_song_whose_bytes_have_expired_says_so(client, populated,
                                                        monkeypatch):
    """Retention deletes on expires_at alone and never touches clips, so a
    shipped song outliving its audio is the expected state, not an error.

    Pinned to the database store: that is the only configuration in which a
    clip with no row means the audio is gone rather than somewhere else.
    """
    monkeypatch.setenv("AUDIO_STORE", "database")
    reload_settings()
    r = client.get("/files")
    assert r.status_code == 200
    assert "<audio controls" not in r.text
    assert 'src="/files/' not in r.text
    assert "no audio kept" in r.text


def test_a_store_the_console_cannot_read_says_so_rather_than_no_audio_kept(
        client, populated, monkeypatch):
    """LocalStore addresses files as file:// paths, which a page served over
    HTTP cannot load. Saying "no audio kept" there asserts something false: the
    bytes exist, this console just cannot hand them out."""
    monkeypatch.setenv("AUDIO_STORE", "local")
    reload_settings()
    body = client.get("/files").text
    assert "no audio kept" not in body
    assert "not served from here" in body
    assert "AUDIO_STORE=database" in body, "an operator needs to know what to change"
    assert "this is where you listen to it" not in body
    assert "file://" not in body


def test_spaces_is_played_through_a_signed_url(client, populated, monkeypatch):
    """The storage the README describes. Its objects are private, so a bare key
    is a 403 — but signing one costs no round trip, so the console can offer a
    player for a catalogue it does not itself hold."""
    for var, val in (("AUDIO_STORE", "spaces"), ("SPACES_KEY", "k"),
                     ("SPACES_SECRET", "s"), ("SPACES_BUCKET", "daily-five"),
                     ("SPACES_ENDPOINT", "https://fra1.digitaloceanspaces.com")):
        monkeypatch.setenv(var, val)
    reload_settings()
    body = client.get("/files").text
    assert "<audio controls" in body
    assert "no audio kept" not in body
    assert "songs/2026-08-27/01_slow-burn/master.mp3?" in body
    assert "X-Amz-Signature=" in body
    assert "&amp;" in body, "a query string in an attribute has to be escaped"
    assert "as delivered" in body, "the day page the pipeline wrote is up there too"


def test_a_page_you_glance_at_shows_a_duration_before_you_press_play(client, delivered):
    """preload="metadata" is what makes the transport read 3:16 instead of
    0:00 / 0:00. The two bounded pages can afford it per song; the catalogue,
    capped at three hundred rows, would be opening three hundred sources."""
    assert 'preload="metadata"' in client.get("/").text
    assert 'preload="metadata"' in client.get("/runs/2026-08-27").text
    catalogue = client.get("/files").text
    assert 'preload="none"' in catalogue
    assert 'preload="metadata"' not in catalogue


def test_the_overview_offers_todays_songs_with_a_rating_control(client, delivered):
    body = client.get("/").text
    assert "Slow Burn in June" in body
    assert "<audio controls" in body, "the daily habit is hear today's five, then rate them"
    assert len(re.findall(r'name="rating"', body)) == 10
    assert 'action="/console/rate"' in body


def test_rating_from_the_console_works_without_javascript(client, populated):
    with session_scope() as s:
        clip_id = s.query(Clip).filter(Clip.shipped.is_(True)).one().id
    r = client.post("/console/rate", follow_redirects=False,
                    data={"clip_id": clip_id, "rating": 8, "back": "/runs/2026-08-27"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/runs/2026-08-27#clip{clip_id}"
    with session_scope() as s:
        assert s.query(Outcome).filter(Outcome.clip_id == clip_id).one().rating == 8


def test_rating_never_redirects_off_this_origin(client, populated):
    """`back` is a form field, and "//host" is a protocol-relative URL."""
    with session_scope() as s:
        clip_id = s.query(Clip).filter(Clip.shipped.is_(True)).one().id
    r = client.post("/console/rate", follow_redirects=False,
                    data={"clip_id": clip_id, "rating": 3, "back": "//evil.example"})
    assert r.headers["location"] == f"/#clip{clip_id}"


def test_a_recorded_rating_is_rendered_by_the_server(client, populated):
    """No localStorage seeding and no hydration pass — the console has the
    database, so the truth is in the HTML before any script runs."""
    with session_scope() as s:
        clip_id = s.query(Clip).filter(Clip.shipped.is_(True)).one().id
    client.post("/console/rate", data={"clip_id": clip_id, "rating": 9})
    body = client.get("/runs/2026-08-27").text
    assert "rated 9/10" in body
    assert 'value="9" aria-pressed="true"' in body


def test_the_delivered_day_page_is_linked_only_when_it_exists(client, delivered):
    """Under Spaces it is private and under LocalStore it is a path on a disk,
    so a constructed href would 404 in two of three configurations."""
    key = "songs/2026-08-27/index.html"
    assert f'href="/files/{key}"' not in client.get("/files").text
    delivered.put_text("<html></html>", key, content_type="text/html")
    assert f'href="/files/{key}"' in client.get("/files").text
    assert f'href="/files/{key}"' in client.get("/runs/2026-08-27").text


def test_the_files_page_never_preloads_three_hundred_sources(client, delivered):
    """This page can list 300 songs; preload="metadata" would open 300 sources
    before anyone pressed play."""
    assert 'preload="none"' in client.get("/files").text
    assert 'preload="metadata"' not in client.get("/files").text


def test_the_agents_page_states_that_cover_art_has_no_key(client):
    """The fact used to live only in an ERROR nobody reads. The Agents page is
    where an operator looks at configuration, and the brain table already has a
    component for exactly this."""
    body = client.get("/agents").text
    assert "Cover art" in body
    assert "modelark" in body and "seedream" in body
    assert "no key" in body


def test_the_files_page_does_not_promise_a_cover(client):
    """Prose cannot 404, which is why it went unnoticed — but it told an
    operator every delivered folder holds a cover.jpg, and none does."""
    assert "cover.jpg" not in client.get("/files").text


def test_clearing_a_rating_from_the_console_works_without_javascript(client, populated):
    """A form cannot issue DELETE, so the clear control is a second submit in
    the same form — and it carries no rating field, which is why console_rate
    has to branch before it parses one."""
    with session_scope() as s:
        clip_id = s.query(Clip).filter(Clip.shipped.is_(True)).one().id
    client.post("/console/rate", data={"clip_id": clip_id, "rating": 8})

    r = client.post("/console/rate", follow_redirects=False,
                    data={"clip_id": clip_id, "clear": "1", "back": "/runs/2026-08-27"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/runs/2026-08-27#clip{clip_id}"
    with session_scope() as s:
        assert s.query(Outcome).filter(Outcome.clip_id == clip_id).one().rating is None


def test_the_clear_control_appears_only_once_there_is_a_rating(client, populated):
    """Offering to undo an unrated song is an invitation to wonder what it
    would do."""
    with session_scope() as s:
        clip_id = s.query(Clip).filter(Clip.shipped.is_(True)).one().id
    assert 'name="clear"' not in client.get("/runs/2026-08-27").text

    client.post("/console/rate", data={"clip_id": clip_id, "rating": 6})
    assert 'name="clear"' in client.get("/runs/2026-08-27").text

    client.post("/console/rate", data={"clip_id": clip_id, "clear": "1"})
    assert 'name="clear"' not in client.get("/runs/2026-08-27").text


def test_a_clear_that_names_no_clip_is_still_refused(client):
    assert client.post("/console/rate", data={"clear": "1"}).status_code == 400


def test_the_console_script_clears_without_reloading(client):
    """Progressive enhancement only — the form posts and redirects on its own —
    but a reload stops whatever is playing mid-song."""
    body = client.get("/").text
    assert "ev.submitter.name === 'clear'" in body
    assert "method: 'DELETE'" in body


def test_the_overview_states_the_shape_the_settings_produce(client, monkeypatch):
    """The sentence that drifted: the console announced a split of full-length and
    short-form long after the slot counts stopped producing one, and nothing
    asserted on it, so it stayed green saying anything at all. Both halves matter —
    the numbers come from settings, and a lane with no slots is not mentioned."""
    from dailyfive.config import reload_settings

    for var, value in (("FULL_BRIEFS", "7"), ("FULL_SLOTS", "5"),
                       ("SHORT_BRIEFS", "0"), ("SHORT_SLOTS", "0")):
        monkeypatch.setenv(var, value)
    reload_settings()
    text = client.get("/").text
    assert ("5 finished songs a day, unattended, all full-length, "
            "chosen from 14 candidates.") in text
    assert "short-form" not in text, "a lane with no slots is not a lane to announce"

    for var, value in (("FULL_BRIEFS", "4"), ("FULL_SLOTS", "3"),
                       ("SHORT_BRIEFS", "3"), ("SHORT_SLOTS", "2")):
        monkeypatch.setenv(var, value)
    reload_settings()
    assert ("5 finished songs a day, unattended. 3 full-length and 2 short-form, "
            "chosen from 14 candidates.") in client.get("/").text
