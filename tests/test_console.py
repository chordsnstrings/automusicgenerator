"""The console must render from any database state, including a broken config.

A dashboard that 500s when something is wrong is useless exactly when it is
needed, so every page is exercised against an empty database as well as a
populated one.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from dailyfive.db import session_scope
from dailyfive.models import (AgentCall, Brief, Clip, Decision, Job, JobState,
                              Run, RunPhase, Signal, SlotType)
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
