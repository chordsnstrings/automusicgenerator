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

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from sqlalchemy import LargeBinary, func, select

from ..config import settings
from ..db import init_db, session_scope
from ..models import Clip, Job, Outcome, Run, StoredFile, utcnow

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
    allow_methods=["GET", "HEAD", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=86400,
)


def _check_secret(secret: str) -> None:
    expected = settings().webhook_secret or "nosecret"
    if not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=404, detail="not found")


@app.api_route("/healthz", methods=["GET", "HEAD"])
def liveness() -> dict:
    """Is the process alive? Nothing more.

    Deliberately separate from /health, which also asks whether the database
    answers and whether today's run shipped. A platform health check wired to
    /health restarts the container whenever the database hiccups, which throws
    away the one process that could have told you what the database said. This
    one only proves the server is serving; /health stays the readiness and
    status endpoint for humans and uptime monitors.
    """
    return {"ok": True}


@app.api_route("/health", methods=["GET", "HEAD"])
def health(response: Response) -> dict:
    """Liveness plus the two things that actually go wrong unattended.

    A health check that only reports "the process is up" is worthless on a
    system whose failure mode is a run that quietly stopped happening. So this
    also answers: can I reach the database, and did today's run actually ship?
    Returns 503 when either is false, so an uptime monitor notices without
    anyone reading a log.
    """
    from datetime import date, timedelta

    out: dict = {"ok": True, "checks": {}}

    try:
        with session_scope() as s:
            latest = s.execute(
                select(Run).order_by(Run.run_date.desc()).limit(1)).scalar_one_or_none()
        out["checks"]["database"] = "ok"
    except Exception as exc:
        response.status_code = 503
        return {"ok": False, "checks": {"database": f"unreachable: {exc}"[:200]}}

    if latest is None:
        out["latest_run"] = None
        out["checks"]["runs"] = "no run has ever completed"
        return out

    out["latest_run"] = latest.run_date.isoformat()
    out["phase"] = latest.phase.value

    age = (date.today() - latest.run_date).days
    if latest.phase.value == "failed":
        out["ok"] = False
        out["checks"]["runs"] = f"the {latest.run_date} run failed: {(latest.error or '')[:160]}"
        response.status_code = 503
    elif age > 1:
        out["ok"] = False
        out["checks"]["runs"] = f"no run since {latest.run_date} ({age} days ago)"
        response.status_code = 503
    elif (latest.notes or {}).get("shortfall"):
        out["checks"]["runs"] = "last run shipped short of contract"
    else:
        out["checks"]["runs"] = "ok"

    return out


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


# The rating endpoint is deliberately open — it is posted from a browser on a
# signed Spaces URL, and an auth step on a control you use for thirty seconds
# every morning does not get used. Open is not the same as unlimited, though:
# without a ceiling a stranger with the URL could rewrite the taste model in an
# afternoon. Per-IP, in memory, reset hourly — enough to stop that without
# adding Redis to a single-droplet deployment.
_RATE_WINDOW_S = 3600
_RATE_MAX = 120
_rate_hits: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def _rate_limited(ip: str) -> bool:
    import time
    now = time.time()
    hits = [t for t in _rate_hits.get(ip, []) if now - t < _RATE_WINDOW_S]
    if len(hits) >= _RATE_MAX:
        _rate_hits[ip] = hits
        return True
    hits.append(now)
    _rate_hits[ip] = hits
    if len(_rate_hits) > 4096:            # unbounded dict is its own outage
        cutoff = now - _RATE_WINDOW_S
        for key in [k for k, v in _rate_hits.items() if not v or max(v) < cutoff]:
            _rate_hits.pop(key, None)
    return False


@app.post("/ratings")
async def submit_rating(request: Request) -> JSONResponse:
    """The field the pipeline cannot fill in for itself.

    Open rather than secret-gated: it is posted from a browser on a signed
    Spaces URL, and the worst a stranger can do is skew your own taste model.
    Weigh that against the friction of an auth step on a control you need to use
    every morning in under thirty seconds.
    """
    if _rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many ratings; try later")

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


@app.delete("/ratings/{clip_id}")
async def clear_rating(clip_id: int, request: Request) -> JSONResponse:
    """Take a rating back.

    DELETE rather than a rating of some sentinel value, because the two are
    genuinely different actions and a 1-10 widget has no spare value to spend:
    both write paths reject 0 as out of range, and a mis-tap being permanent is
    what makes the widget a trap. The rating feeds the Archivist's learning
    signal and the weekly retro, so a wrong value does not merely sit there — it
    steers the system until it is removed.

    Open for the same reason POST /ratings is, and with less at stake: the note
    survives a clear, so this is the least destructive thing an endpoint on this
    path can do.
    """
    if _rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many ratings; try later")

    with session_scope() as s:
        if s.get(Clip, clip_id) is None:
            raise HTTPException(status_code=404, detail="no such clip")

    from ..archivist import unrate
    return JSONResponse({"ok": True, "clip_id": clip_id, "cleared": unrate(clip_id)})


@app.api_route("/ratings", methods=["GET", "HEAD"])
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


CHUNK = 1 << 20


def _chunks(key: str, start: int, end: int):
    """Yield the requested extent a slice at a time.

    Reading `data` as a whole column is what made this endpoint dangerous: it is
    an undeferred LargeBinary, a master.wav is tens of MB, and on Postgres a
    bytea arrives hex-encoded, so one 60 MB row cost ~180 MB of a 512 MB box
    before the range was even parsed. substr() reads only the slice asked for on
    both dialects. A fresh session per slice rather than one held open for the
    whole response: a listener on bad hotel wifi would otherwise hold a pooled
    connection for minutes, and the pool is 15.
    """
    pos = start
    while pos <= end:
        n = min(CHUNK, end - pos + 1)
        with session_scope() as s:
            blob = s.execute(
                select(func.substr(StoredFile.data, pos + 1, n, type_=LargeBinary))
                .where(StoredFile.key == key)).scalar_one_or_none()
        if not blob:                  # purged mid-transfer; stop cleanly
            return
        yield blob
        pos += len(blob)


@app.api_route("/files/{key:path}", methods=["GET", "HEAD"])
def serve_file(key: str, request: Request) -> Response:
    """Stream a delivered file out of the database.

    Range support is not optional here. An <audio> element issues a ranged
    request to seek, and a server that answers 200-with-everything makes the
    scrub bar dead — the player can only ever play from the start. Chrome will
    also refuse to show a duration for a non-ranged stream of unknown length.
    """
    with session_scope() as s:
        meta = s.execute(
            select(func.length(StoredFile.data), StoredFile.content_type,
                   StoredFile.sha256)
            .where(StoredFile.key == key,
                   (StoredFile.expires_at.is_(None))
                   | (StoredFile.expires_at > utcnow()))).one_or_none()
    if meta is None:
        raise HTTPException(status_code=404, detail="no such file")
    # func.length over size_bytes so the advertised length cannot drift from the
    # bytes substr() will actually hand back.
    total, ctype, etag = int(meta[0] or 0), meta[1], meta[2]

    # A quote in the name would close the header's quoted-string early.
    name = key.rsplit("/", 1)[-1].replace('"', "")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{name}"',
        # Immutable within its retention window, so let the browser keep it.
        "Cache-Control": "private, max-age=86400",
        # This origin serves LLM-authored text/html out of stored_files beside
        # the console, so a guessed content type is a scripting bug.
        "X-Content-Type-Options": "nosniff",
    }
    if etag:
        headers["ETag"] = f'"{etag}"'
        # Answered before a single byte is read — that is the whole point of
        # doing it here. If-Range needs no handling for the same reason the
        # validator is trustworthy: the ETag is the sha256 of the bytes,
        # recomputed on every write, so it cannot survive a change to them.
        if request.headers.get("if-none-match") == f'"{etag}"':
            return Response(status_code=304, headers=headers)

    if request.method == "HEAD":
        # Returning here rather than falling through is the entire fix. Starlette
        # does not strip a body on HEAD — it runs the generator to exhaustion and
        # hands every chunk to the ASGI layer, and it is uvicorn, one hop further
        # out, that discards them. Falling through would therefore cost ~60
        # substr() round trips and ~60 MB through a 512 MB container to produce a
        # Content-Length already computed above, which is strictly worse than the
        # 405 this replaced. Range is ignored deliberately: RFC 9110 §14.2 defines
        # range handling for GET only, so a HEAD answers 200 with the length of
        # the whole representation and never 206 or 416. That is also the only
        # honest answer, since a HEAD asks how big the file is and reporting a
        # slice length would misstate it. Accept-Ranges is already in headers, so
        # the client still learns it may range the GET that follows.
        headers["Content-Length"] = str(total)
        return Response(status_code=200, media_type=ctype, headers=headers)

    rng = request.headers.get("range")
    start, end = 0, total - 1
    partial = False
    if rng and rng.startswith("bytes="):
        # Media elements never ask for more than one range, and Content-Range
        # states honestly which one came back.
        spec = rng[6:].split(",")[0].strip()
        start_s, _, end_s = spec.partition("-")
        try:
            if start_s:
                start = int(start_s)
                end = int(end_s) if end_s else total - 1
            else:
                # A suffix range: the last N bytes.
                start, end = max(0, total - int(end_s)), total - 1
        except ValueError:
            # RFC 9110 §14.2: an unparseable Range is ignored, not refused. A
            # 416 here would break a client over a header it could survive.
            start, end, partial = 0, total - 1, False
        else:
            if start >= total or start > end:
                return Response(status_code=416,
                                headers={**headers, "Content-Range": f"bytes */{total}"})
            end = min(end, total - 1)
            partial = True

    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return StreamingResponse(_chunks(key, start, end),
                             status_code=206 if partial else 200,
                             media_type=ctype, headers=headers)


@app.api_route("/storage", methods=["GET", "HEAD"])
def storage_usage() -> dict:
    """What the store holds and where it settles. Cheap enough to poll."""
    from ..retention import usage
    return usage()


@app.api_route("/ratings/status", methods=["GET", "HEAD"])
def rating_status() -> dict:
    from ..archivist import learning_status
    return learning_status()


# ── console ──────────────────────────────────────────────────────────────────
# The day page shows five songs; these show the machine that made them. Kept on
# the same service because it is the same database and the same one public
# surface to secure.

@app.api_route("/runs", methods=["GET", "HEAD"], response_class=HTMLResponse)
def console_runs() -> str:
    from . import views
    return views.runs_list()


@app.api_route("/runs/{run_date}", methods=["GET", "HEAD"], response_class=HTMLResponse)
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


@app.api_route("/agents", methods=["GET", "HEAD"], response_class=HTMLResponse)
def console_agents() -> str:
    from . import views
    return views.agents_page()


@app.api_route("/codex", methods=["GET", "HEAD"], response_class=HTMLResponse)
def console_codex() -> str:
    from . import views
    return views.codex_page()


@app.api_route("/files", methods=["GET", "HEAD"], response_class=HTMLResponse)
def console_files() -> str:
    from . import views
    return views.files_page()


@app.post("/console/rate")
async def console_rate(request: Request) -> Response:
    """The no-JavaScript path for the console's rating buttons.

    Form-encoded rather than JSON because a plain <form> is what still works when
    the enhancement script does not run; the body is parsed by hand because
    adding python-multipart as a dependency to read two integers is not a trade
    worth making. POST-redirect-GET so a refresh does not re-submit.
    """
    from urllib.parse import parse_qs

    if _rate_limited(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many ratings; try later")

    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    # The clear button is a second submit inside the same form, so its
    # submission carries no rating field at all. Branching after parsing would
    # 400 on every clear.
    clearing = bool(form.get("clear"))
    try:
        clip_id = int(form.get("clip_id", [""])[0])
        rating = 0 if clearing else int(form.get("rating", [""])[0])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="clip_id and rating must be integers")
    if not clearing and not 1 <= rating <= 10:
        raise HTTPException(status_code=400, detail="rating must be 1-10")

    with session_scope() as s:
        if s.get(Clip, clip_id) is None:
            raise HTTPException(status_code=404, detail="no such clip")

    if clearing:
        from ..archivist import unrate
        unrate(clip_id)
    else:
        from ..archivist import rate
        rate(clip_id, rating)

    # Never redirect off this origin on the strength of a form field: "//host"
    # is a protocol-relative URL, not a path on this site.
    back = form.get("back", ["/"])[0]
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    return RedirectResponse(f"{back}#clip{clip_id}", status_code=303)


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def root() -> str:
    from . import views
    return views.overview()
