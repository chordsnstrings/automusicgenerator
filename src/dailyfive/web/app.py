"""FastAPI receiver.

Two kinds of traffic land here and nothing else: Suno's generation and WAV
callbacks, and the rating control on each day's delivered page. Keeping them on
one service means one TLS certificate, one firewall rule and one thing to
monitor.

The webhook path carries a shared secret as a path segment. That is deliberate
rather than lazy: a query string ends up in access logs and referrer headers,
while a path prefix can be excluded from logging in one nginx directive.

Callbacks are *hints*, not truth. Every one is folded in through the same
:meth:`Conductor.ingest_record` the poller uses, so a lost callback changes
nothing except how quickly the run notices.
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from ..config import settings
from ..db import init_db, session_scope
from ..models import Clip, Job, Outcome, Run

log = logging.getLogger(__name__)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    log.info("receiver up; callbacks at /webhooks/<secret>/{generate,wav,lyrics}")
    yield


app = FastAPI(title="The Daily Five", docs_url=None, redoc_url=None, lifespan=_lifespan)

# The rating control is the loop-closing mechanism, and it runs on a page served
# from Spaces — a different origin from this receiver. A cross-origin POST with
# a JSON content type triggers a preflight, so without these headers the browser
# blocks every rating and the page reports a save failure with no server-side
# trace at all. Ratings carry no credentials and no cookies, so a permissive
# origin here grants nothing beyond what the endpoint already allows.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=86400,
)


def _check_secret(secret: str) -> None:
    expected = settings().webhook_secret or "nosecret"
    if not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=404, detail="not found")


@app.get("/health")
def health() -> dict:
    with session_scope() as s:
        runs = s.execute(select(Run).order_by(Run.run_date.desc()).limit(1)).scalar_one_or_none()
        return {
            "ok": True,
            "latest_run": runs.run_date.isoformat() if runs else None,
            "phase": runs.phase.value if runs else None,
        }


@app.post("/webhooks/{secret}/generate")
async def generate_callback(secret: str, request: Request) -> JSONResponse:
    """Suno's music-generation callback.

    Always answers 200 quickly. Suno gives up after three consecutive delivery
    failures, and a slow or erroring response here is what causes those — better
    to acknowledge and let the poller reconcile than to hold the connection open
    doing work.
    """
    _check_secret(secret)
    body = await request.json()
    data = body.get("data") or {}
    task_id = data.get("task_id") or data.get("taskId")
    if not task_id:
        log.warning("generate callback with no task_id: %s", str(body)[:300])
        return JSONResponse({"ok": True, "note": "no task_id"})

    with session_scope() as s:
        job = s.execute(select(Job).where(Job.task_id == str(task_id))).scalar_one_or_none()
        if job is None:
            log.warning("callback for unknown task %s", task_id)
            return JSONResponse({"ok": True, "note": "unknown task"})
        job_id, run_id = job.id, job.run_id

    record = {
        "status": data.get("callbackType") and _phase_to_status(data.get("callbackType"))
                  or body.get("status") or data.get("status"),
        "response": {"sunoData": data.get("data") or data.get("sunoData") or []},
        "errorMessage": body.get("msg"),
    }

    try:
        from ..conductor import Conductor
        Conductor(run_id).ingest_record(job_id, record, source="callback")
    except Exception:
        # Never fail the callback on our own error — the poller is the backstop.
        log.exception("callback ingestion failed for task %s", task_id)
    return JSONResponse({"ok": True})


def _phase_to_status(callback_type: str | None) -> str | None:
    """Suno signals progress through callbackType on the callback path and
    through status on the polling path. Normalise to the polling vocabulary so
    both routes converge on one state machine."""
    return {
        "text": "TEXT_SUCCESS",
        "first": "FIRST_SUCCESS",
        "complete": "SUCCESS",
        "error": "GENERATE_AUDIO_FAILED",
    }.get((callback_type or "").lower())


@app.post("/webhooks/{secret}/wav")
async def wav_callback(secret: str, request: Request) -> JSONResponse:
    _check_secret(secret)
    body = await request.json()
    data = body.get("data") or {}
    task_id = data.get("task_id") or data.get("taskId")
    url = data.get("audio_wav_url") or data.get("audioWavUrl")
    if task_id and url:
        with session_scope() as s:
            clip = s.execute(
                select(Clip).where(Clip.wav_task_id == str(task_id))).scalar_one_or_none()
            if clip:
                clip.wav_url = url
                log.info("wav ready for clip %d", clip.id)
    return JSONResponse({"ok": True})


@app.post("/webhooks/{secret}/lyrics")
async def lyrics_callback(secret: str, request: Request) -> JSONResponse:
    """The Suno lyrics endpoint is a fallback path only; acknowledged and logged."""
    _check_secret(secret)
    body = await request.json()
    log.info("lyrics callback: %s", str(body)[:300])
    return JSONResponse({"ok": True})


@app.post("/ratings")
async def submit_rating(request: Request) -> JSONResponse:
    """The field the pipeline cannot fill in for itself.

    Open rather than secret-gated: it is posted from a browser on a signed
    Spaces URL, and the worst a stranger can do is skew your own taste model.
    Weigh that against the friction of an auth step on a control you need to use
    every morning in under thirty seconds.
    """
    body = await request.json()
    clip_id = body.get("clip_id")
    rating = body.get("rating")
    if clip_id is None or rating is None:
        raise HTTPException(status_code=400, detail="clip_id and rating are required")
    try:
        clip_id, rating = int(clip_id), int(rating)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="clip_id and rating must be integers")
    if not 1 <= rating <= 10:
        raise HTTPException(status_code=400, detail="rating must be 1-10")

    with session_scope() as s:
        if s.get(Clip, clip_id) is None:
            raise HTTPException(status_code=404, detail="no such clip")

    from ..archivist import rate
    rate(clip_id, rating, note=(body.get("note") or None))
    return JSONResponse({"ok": True, "clip_id": clip_id, "rating": rating})


@app.get("/ratings")
def existing_ratings(clip_ids: str = "") -> dict:
    """Ratings already recorded for these clips.

    The day page hydrates from this on load. Without it the page would only
    know what is in *this* browser's localStorage — so a song rated on a phone
    reads as unrated on a laptop, and you rate it twice.
    """
    wanted: list[int] = []
    for raw in clip_ids.split(",")[:100]:
        raw = raw.strip()
        if raw.isdigit():
            wanted.append(int(raw))
    if not wanted:
        return {"ratings": {}}
    with session_scope() as s:
        rows = s.execute(
            select(Outcome.clip_id, Outcome.rating)
            .where(Outcome.clip_id.in_(wanted),
                   Outcome.rating.isnot(None))).all()
    return {"ratings": {str(cid): rating for cid, rating in rows}}


@app.get("/ratings/status")
def rating_status() -> dict:
    from ..archivist import learning_status
    return learning_status()


# ── console ──────────────────────────────────────────────────────────────────
# The day page shows five songs; these show the machine that made them. Kept on
# the same service because it is the same database and the same one public
# surface to secure.

@app.get("/runs", response_class=HTMLResponse)
def console_runs() -> str:
    from . import views
    return views.runs_list()


@app.get("/runs/{run_date}", response_class=HTMLResponse)
def console_run(run_date: str) -> str:
    from datetime import datetime
    from . import views
    try:
        parsed = datetime.strptime(run_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    body = views.run_detail(parsed)
    if body is None:
        raise HTTPException(status_code=404, detail=f"no run for {run_date}")
    return body


@app.get("/agents", response_class=HTMLResponse)
def console_agents() -> str:
    from . import views
    return views.agents_page()


@app.get("/codex", response_class=HTMLResponse)
def console_codex() -> str:
    from . import views
    return views.codex_page()


@app.get("/files", response_class=HTMLResponse)
def console_files() -> str:
    from . import views
    return views.files_page()


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    from . import views
    return views.overview()
