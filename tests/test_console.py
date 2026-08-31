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

PAGES = ["/", "/runs", "/agents", "/genres", "/codex", "/files"]


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


def test_the_console_offers_an_undo_the_moment_a_rating_is_made(client):
    """The clear control is server-rendered only where there is a rating, which
    leaves the second after a mis-tap — the one second anybody wants an undo —
    with no control at all until a full reload. Avoiding the reload is the whole
    reason this script exists."""
    body = client.get("/").text
    assert "createElement('button')" in body
    assert "undo.name = 'clear'" in body
    assert "!form.querySelector('button[name=clear]')" in body, \
        "re-rating must not stack a second undo"


def test_a_failed_clear_leaves_the_rating_showing(client):
    """A clear that did not happen and looks like it did hides a value still
    steering the codex, and removes the control that would fix it."""
    body = client.get("/").text
    rate_js = body[body.index("ev.submitter.name === 'clear'"):]
    ok = rate_js.index("if (!r.ok) throw")
    assert rate_js.index("aria-pressed', 'false'") > ok
    assert rate_js.index("ev.submitter.remove()") > ok


def test_the_run_page_says_why_a_day_shipped_without_artwork(client, populated):
    """The pipeline stopped logging an ERROR per song for an unconfigured
    provider and wrote the reason onto the run instead — beside "shortfall",
    which this page already renders. Nothing read it, so an operator who
    noticed the missing covers and opened the run got no explanation."""
    with session_scope() as s:
        run = s.get(Run, populated)
        run.notes = {**(run.notes or {}),
                     "art": {"configured": False, "reason": "ARK_API_KEY unset"}}

    body = client.get("/runs/2026-08-27").text
    assert "No cover art" in body
    assert "ARK_API_KEY unset" in body
    assert "border-left-color:var(--bad)" not in body, \
        "an unconfigured optional provider is not a failed contract"


def test_a_run_with_artwork_configured_says_nothing_about_it(client, populated):
    with session_scope() as s:
        run = s.get(Run, populated)
        run.notes = {**(run.notes or {}), "art": {"configured": True}}
    assert "No cover art" not in client.get("/runs/2026-08-27").text


# ── the genre page ───────────────────────────────────────────────────────────
def _genre_days(family, specific, ratings, *, start=400):
    """One rated brief per day, as the pipeline produces them."""
    from datetime import timedelta

    from dailyfive.models import Outcome, utcnow
    for i, score in enumerate(ratings):
        with session_scope() as s:
            r = Run(run_date=date(2026, 1, 1) + timedelta(days=start + i),
                    phase=RunPhase.SHIPPED)
            s.add(r); s.flush(); rid = r.id
        with session_scope() as s:
            b = Brief(run_id=rid, slot_type=SlotType.FULL, idx=0, title=f"t{i}",
                      theme="a specific situation", genre_family=family,
                      genre=specific, style_string="close-mic vocal")
            s.add(b); s.flush()
            j = Job(run_id=rid, brief_id=b.id, idempotency_key=f"c{rid}",
                    state=JobState.MIRRORED, payload={})
            s.add(j); s.flush()
            first = None
            for v in (0, 1):
                c = Clip(run_id=rid, job_id=j.id, brief_id=b.id,
                         audio_id=f"c{rid}-{v}", variant=v, slot_type=SlotType.FULL,
                         genre_family=family, genre=specific, shipped=v == 0,
                         qc_verdict="pass", score_total=6.0)
                s.add(c); s.flush()
                first = first or c.id
            if score is not None:
                s.add(Outcome(clip_id=first, rating=score, rated_at=utcnow()))


def test_the_genre_page_is_the_vocabulary_before_it_is_a_scoreboard(client):
    """Day one has to be a real page, not an empty one. It shows what the
    studio knows how to make and says plainly that it has made none of it."""
    from dailyfive.genres import FAMILIES, SPECIFICS
    body = client.get("/genres").text
    for family in FAMILIES:
        assert family in body, family
    for specific in ("country-soul", "bedroom-pop", "uk-garage", "bachata"):
        assert specific in body, specific
    assert f"{len(FAMILIES)} families" in body
    assert f"{len(SPECIFICS)} specific genres" in body
    assert "has recorded none of them" in body


def test_the_genre_page_prints_no_mean_before_the_bar_is_cleared(client):
    """Cold, and cold has to look like cold. Seven rated briefs is a count and
    a dash, never an average."""
    import re

    from dailyfive.genres import GENRE_MIN_RATED
    _genre_days("country", "country-soul", [9] * (GENRE_MIN_RATED - 1))
    body = client.get("/genres").text
    cells = re.findall(r"<td>(.*?)</td>", body, flags=re.S)
    means = [c for c in cells if "rated brief" in c]
    assert means, "the mean column disappeared"
    for cell in means:
        assert "<b>" not in cell, f"a mean was printed: {cell}"
        assert f"of {GENRE_MIN_RATED} rated briefs" in cell
    assert "nothing here is ranked" in body


def test_the_genre_page_states_the_confound_in_plain_words(client):
    body = client.get("/genres").text
    for phrase in ("entangled with persona", "not a controlled experiment",
                   "one rater", "Nothing is blinded", "Marisol"):
        assert phrase in body, phrase


def test_the_genre_page_says_which_briefs_predate_the_column(client, run_id,
                                                             brief_factory):
    """The six briefs already in production keep a null. The page has to say
    so without implying an error, because there is nothing to fix."""
    brief_factory(idx=0)
    brief_factory(idx=1)
    body = client.get("/genres").text
    assert "2 briefs carry no genre at all" in body
    assert "before the studio recorded one" in body
    assert "there is nothing to fix" in body


def test_the_genre_page_ranks_on_your_rating_and_not_the_producers(client):
    from dailyfive.genres import GENRE_MIN_RATED
    _genre_days("folk", "indie-folk", [9] * GENRE_MIN_RATED, start=400)
    _genre_days("pop", "alt-pop", [4] * GENRE_MIN_RATED, start=440)
    body = client.get("/genres").text
    assert "folk is ahead" in body
    assert body.index("<b>folk</b>") < body.index("<b>pop</b>")
    assert "never used to order this table" in body


def test_the_genre_page_shows_a_slate_on_a_day_no_run_has_happened(client):
    """The allocator is deterministic, so the slate a fresh install would be
    given is a real thing to show — and it says what each pick is."""
    from dailyfive.config import settings
    cfg = settings()
    body = client.get("/genres").text
    assert "The next slate" in body
    explore = cfg.total_briefs - min(cfg.genre_rhythm_floor, cfg.total_briefs // 2)
    assert f"{explore} of {cfg.total_briefs} briefs are exploration" in body


def test_the_genre_page_says_the_rhythm_floor_is_taste_and_not_evidence(client):
    """The floor overrides the ratings on purpose, so the page that exists to
    report what the studio knows must not let it read as something learned. It
    is counted separately from exploration for the same reason: an exploration
    pick is the allocator admitting it does not know, a rhythm pick is the
    operator deciding regardless."""
    from dailyfive.config import settings
    body = client.get("/genres").text
    if not settings().genre_rhythm_floor:
        pytest.skip("no rhythm floor configured")
    assert "rhythm floor" in body
    assert "taste decision, not a measurement" in body
    assert "biases the record" in body


def test_the_genre_page_keeps_the_two_chart_scopes_apart(client, run_id):
    with session_scope() as s:
        s.get(Run, run_id).notes = {"scout": {"external_genres": {
            "current": {"country": 6, "pop": 6},
            "catalogue": {"country": 17, "hip-hop": 14},
            "deezer": {"pop": 18, "country": 10},
            "entries": {"apple_current": 19, "apple_catalogue": 31, "deezer": 25},
            "sources": ["apple", "deezer"]}}}
    body = client.get("/genres").text
    assert "country and pop tied on 6" in body, "a 6-6 tie was broken silently"
    assert "country on 23" in body
    assert "hold confidence down" in body


def test_the_roster_count_is_computed(client):
    from dailyfive.web.views import ROSTER
    body = client.get("/agents").text
    assert f"{len(ROSTER)} roles" in body
    assert "Eleven roles" not in body, "a hardcoded count beside a computed one"
    assert "Genre Director" in body


def test_the_overview_points_at_the_genre_record(client):
    body = client.get("/").text
    assert 'href="/genres"' in body
    assert "Nothing briefed yet" in body


def test_the_codex_page_never_shows_a_negation_as_something_that_scored_well(client):
    """The live codex reached v3 with a learned table that was entirely
    "no synths" 6.11 and "no pads" 6.11, and a version is never rewritten in
    place, so those rows are in the record forever. The page they are rendered
    on must not repeat what the prompt was about to do with them."""
    import json

    from dailyfive.codex import SEED_CODEX, save_new_version
    body = json.loads(json.dumps(SEED_CODEX))
    body["learned"]["style_scores"] = {"no synths": 6.11, "no pads": 6.11,
                                       "brushed kit": 7.4}
    save_new_version(body, [], diff="test", rationale="test")

    page = client.get("/codex").text
    assert "brushed kit" in page
    assert "no synths" not in page and "no pads" not in page
    assert "2 stored descriptors not shown" in page


def test_the_codex_page_shows_a_genre_score_with_its_sample_count(client):
    import json

    from dailyfive.codex import SEED_CODEX, save_new_version
    body = json.loads(json.dumps(SEED_CODEX))
    body["learned"]["genre_scores"] = {"country": {"mean": 7.4, "n": 12}}
    save_new_version(body, [], diff="test", rationale="test")

    page = client.get("/codex").text
    assert "7.40" in page and "over 12 rated briefs" in page


def test_the_two_pages_never_name_different_leaders(client):
    """Two families on identical numbers used to make the overview and /genres
    disagree: one sorted on (taste, name) and the other on (-taste, name), so
    the alphabetically last and first names won respectively. The overview
    links straight through, so a reader followed a link that reversed the
    answer — and neither page said the two were level."""
    from dailyfive.genres import GENRE_MIN_RATED
    _genre_days("country", "country-soul", [7] * GENRE_MIN_RATED, start=400)
    _genre_days("pop", "alt-pop", [7] * GENRE_MIN_RATED, start=440)

    overview, page = client.get("/").text, client.get("/genres").text
    assert "country and pop are level at 7.0" in overview
    assert "country and pop are level at 7.0" in page
    for body in (overview, page):
        assert "is ahead" not in body
        assert "trails" not in body
        assert "leads at" not in body


def test_the_genre_page_does_not_deny_the_ratings_it_just_reported(client):
    """The steady state this design is aiming at: every family carrying a
    rating has cleared the bar, so nothing is part-way to it. That used to fall
    through to the branch asserting nothing had been rated at all, printed
    inches under a headline naming a leader."""
    from dailyfive.genres import GENRE_MIN_RATED
    _genre_days("country", "country-soul", [7] * GENRE_MIN_RATED, start=400)
    _genre_days("pop", "alt-pop", [7] * GENRE_MIN_RATED, start=440)

    body = client.get("/genres").text
    assert "none of them has been rated" not in body
    assert "Every family carrying a rating has cleared it" in body
    assert f"2 of 9 ranked" in body
    assert "7 briefed and waiting on you" not in body   # none of the other 7 was briefed
    assert "7 waiting on a slate" in body


def test_the_producer_mean_is_never_a_bare_number(client):
    """The one number available on day one, in the row the table floats to the
    top, and an average over CLIPS where every other count on the row is
    briefs. Two clips of one brief is one decision, which is the whole error
    this page was built to stop repeating."""
    _genre_days("country", "country-soul", [None])
    with session_scope() as s:
        for c in s.query(Clip).all():
            c.score_total = 9.4

    body = client.get("/genres").text
    assert "9.4" in body
    assert "over 2 scored clips" in body


def test_the_overview_does_not_call_a_pre_genre_database_empty(client, run_id,
                                                               brief_factory):
    """Production's state the day this shipped: six briefs and twelve clips,
    all predating the column. "Nothing briefed yet" would be the one line on
    the console claiming the studio has made nothing."""
    brief_factory(idx=0)
    body = client.get("/").text
    assert "Nothing briefed yet" not in body
    assert "1 brief carry no genre" in body or "1 brief carries no genre" in body


def test_the_next_slate_preview_is_the_slate_the_run_would_be_given(client, run_id):
    """The preview called the allocator with no chart evidence while the
    pipeline calls it with today's. _coverage_pick sorts a family the charts
    named ahead of one they did not, among families sampled equally — which on
    an empty database is the entire ordering, so the preview showed a slate the
    run would not brief, under a caption saying it was the same one."""
    from dailyfive import genres
    from dailyfive.config import settings
    sheet = {"external_genres": {
        "current": {"country": 6, "pop": 6, "hip-hop": 4, "latin": 2,
                    "r-and-b": 2, "alternative": 1, "electronic": 1},
        "deezer": {"pop": 18, "country": 10},
        "entries": {"apple_current": 19, "apple_catalogue": 31, "deezer": 25},
        "sources": ["apple", "deezer"]}}
    with session_scope() as s:
        s.get(Run, run_id).notes = {"scout": sheet}

    expected = genres.slate(settings().total_briefs,
                            calibration=None,
                            external=sheet["external_genres"])
    body = client.get("/genres").text
    assert "The next slate" in body
    for row in expected:
        assert f"<b>{row['genre_family']}</b>" in body, row["genre_family"]
    # And the families the allocator did NOT pick are absent from the slate
    # table, which is the half that used to be wrong.
    picked = {r["genre_family"] for r in expected}
    slate_html = body[body.index("The next slate"):body.index("The last 30 days")]
    for family in genres.FAMILIES:
        if family not in picked:
            assert f"<b>{family}</b>" not in slate_html, family


def test_the_day_one_page_counts_one_brief_as_one_brief(client, run_id,
                                                        brief_factory):
    """Singular counts with plural nouns, on the first page a new operator
    opens. The same module already gets this right three hundred lines away."""
    _genre_days("country", "country-soul", [None])
    body = client.get("/genres").text
    assert "1 briefs" not in body
    assert "across 1 brief," in body


def test_the_vocabulary_pressure_note_names_the_run_and_the_word(client, run_id):
    """A count that climbs is the evidence a term is missing — but only the
    word says WHICH term, and only the date says whether "the last run" means
    today or three days ago, which it does whenever a run fails before it
    writes a slate."""
    with session_scope() as s:
        s.get(Run, run_id).notes = {"genre": {
            "slate": [{"genre_family": "pop", "specs": 7, "stance": "explore",
                       "mean": None, "n": 0, "why": "coverage: never briefed"}],
            "off_vocabulary": ["afrobeats"], "unlabelled": 1}}
        day = s.get(Run, run_id).run_date.isoformat()

    body = client.get("/genres").text
    assert f"outside the vocabulary 1 time on the run of {day}" in body
    assert "afrobeats" in body
    assert "on the last run" not in body


def test_the_codex_page_survives_a_genre_score_stored_as_a_bare_number(client):
    """A codex version is never rewritten in place, so every shape this key
    has ever carried stays readable forever — codex.scored() is written to
    tolerate them and brief_context() renders this body fine. The console must
    not be stricter than the prompt: it used to index every stored value as a
    dict while sorting, and took the page down on a body the Director briefs
    from happily."""
    import json

    from dailyfive.codex import SEED_CODEX, save_new_version
    body = json.loads(json.dumps(SEED_CODEX))
    body["learned"]["genre_scores"] = {"country": 7.4,
                                       "pop": {"mean": 6.0, "n": 9},
                                       "folk": {"mean": None}}
    save_new_version(body, [], diff="test", rationale="test")

    r = client.get("/codex")
    assert r.status_code == 200
    assert "7.40" in r.text and "over 0 rated briefs" in r.text
    assert "6.00" in r.text and "over 9 rated briefs" in r.text
    assert "<b>folk</b>" not in r.text, "a row with no mean was given one"


def test_the_codex_page_says_the_real_reason_its_learned_table_is_empty(client):
    """The deployed v3 body exactly: two descriptors, both negations, both well
    over the observation floor. "No descriptor has enough observations behind
    it yet" is false about the only two rows there are, and it sat directly
    above a note giving the correct, different reason."""
    import json

    from dailyfive.codex import SEED_CODEX, save_new_version
    body = json.loads(json.dumps(SEED_CODEX))
    body["learned"]["style_scores"] = {"no synths": 6.11, "no pads": 6.11}
    save_new_version(body, [], diff="test", rationale="test")

    page = client.get("/codex").text
    assert "no descriptor has enough observations behind it yet" not in page
    assert "every one of the 2 stored descriptors is a negation" in page


def test_the_deploy_smoke_test_asks_for_every_console_page():
    """The one script that checks production after a deploy enumerates the
    pages by hand, so a new route is covered only if someone remembers. /genres
    was not, and it is the heaviest page in the console."""
    from pathlib import Path

    from dailyfive.web.console import NAV
    script = (Path(__file__).resolve().parents[1]
              / "deploy" / "verify-production.sh").read_text()
    line = next(ln for ln in script.splitlines() if ln.startswith("for p in "))
    asked = set(line.removeprefix("for p in ").split(";")[0].split())
    missing = {href for href, _ in NAV} - asked
    assert not missing, f"verify-production.sh never requests {sorted(missing)}"


def test_the_genre_page_names_the_languages_this_container_can_render(client):
    """Otherwise unobservable. languages.available() filters the roster by
    asking fontconfig on the machine that renders, so an image whose font
    packages failed to install does not crash or warn — it quietly ships
    English-only days, and the first sign would be a month of English songs for
    no stated reason."""
    from dailyfive.config import settings
    body = client.get("/genres").text
    if not settings().language_briefs:
        assert "switched off" in body
        return
    assert "second language" in body
    assert "Spanish" in body and "Korean" in body and "Arabic" in body


def test_the_genre_page_says_so_loudly_when_a_font_is_missing(client, monkeypatch):
    from dailyfive import languages
    from dailyfive.web import views

    only_latin = tuple(lg for lg in languages.LANGUAGES if lg.script == "Latin")
    monkeypatch.setattr(views.languages, "available", lambda: only_latin)
    body = client.get("/genres").text
    assert "missing a font package" in body
    assert "Korean" in body


def test_the_genre_page_survives_every_font_being_gone(client, monkeypatch):
    from dailyfive.web import views
    monkeypatch.setattr(views.languages, "available", lambda: ())
    r = client.get("/genres")
    assert r.status_code == 200
    assert "English-only days rather than boxes" in r.text


def test_a_failed_short_says_why_on_the_run_page(client, populated):
    """A scheduler slot that raises is caught, logged and forgotten, so a failed
    video left the page looking exactly like a day nobody asked for one. "The
    video allowance was spent" is actionable; "no short today" is not."""
    from dailyfive.db import session_scope
    from dailyfive.models import Run
    with session_scope() as s:
        run = s.get(Run, populated)
        run.notes = {**(run.notes or {}), "short": {
            "ok": False, "clip_id": 1,
            "error": "[minimax-video] plan usage limit reached — the day's "
                     "video allowance is spent"}}
    body = client.get("/runs/2026-08-27").text
    assert "No short" in body
    assert "allowance is spent" in body


def test_a_built_short_says_so_and_that_posting_is_manual(client, populated):
    from dailyfive.db import session_scope
    from dailyfive.models import Run
    with session_scope() as s:
        run = s.get(Run, populated)
        run.notes = {**(run.notes or {}), "short": {
            "ok": True, "clip_id": 1,
            "result": {"performer": "amara", "duration_s": 20.0,
                       "clips": ["clip0.mp4", "clip1.mp4"]}}}
    body = client.get("/runs/2026-08-27").text
    assert "Short built" in body
    assert "amara" in body and "2 takes" in body
    assert "Publishing is manual" in body


def test_a_day_with_no_short_attempt_shows_nothing(client, populated):
    """Silence is right when nothing was tried — the note exists to explain an
    attempt, not to advertise a feature."""
    body = client.get("/runs/2026-08-27").text
    assert "No short" not in body and "Short built" not in body
