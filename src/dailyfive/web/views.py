"""Console page bodies. Each reads the database and returns HTML."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta

from sqlalchemy import func, select

from .. import ledger, llm
from ..archivist import aggregate, learning_status
from ..codex import current as current_codex
from ..config import settings
from ..db import session_scope
from ..models import (AgentCall, Brief, Clip, CodexVersion, Decision, Job,
                      Outcome, Run, RunPhase, Signal, StoredFile, utcnow)
from .console import (RATE_JS, ago, bar, dur, esc, jsonblock, ms, page, pill,
                      stats, table)

log = logging.getLogger(__name__)

PHASES = [RunPhase.SENSED, RunPhase.BRIEFED, RunPhase.WRITTEN, RunPhase.SUBMITTED,
          RunPhase.RENDERED, RunPhase.JUDGED, RunPhase.SHIPPED]
PHASE_LABEL = {RunPhase.SENSED: "sense", RunPhase.BRIEFED: "brief",
               RunPhase.WRITTEN: "write", RunPhase.SUBMITTED: "submit",
               RunPhase.RENDERED: "render", RunPhase.JUDGED: "judge",
               RunPhase.SHIPPED: "ship"}

# The full roster, including the four that deliberately have no brain.
ROSTER = [
    ("Scout", "scout", "trend + sentiment signals from seven free feeds",
     "making music for a mood that peaked three weeks ago"),
    ("Music Director", "director", "owns the Style Codex; themes become checkable spec",
     "prompts made of adjectives instead of specifications"),
    ("A&R", "anr", "seven typed briefs, personas assigned, diversity enforced",
     "five songs turning out to be the same song"),
    ("Lyricist", "lyricist", "two drafts per brief, then a forced choice",
     "generic AI lyric mush"),
    ("Clearance", "clearance", "deterministic blocklist, then a model pass",
     "a moderation rejection burning credits at 2am"),
    ("Prompt Compiler", None, "brief becomes a validated Suno payload",
     "silent truncation and invalid parameter combinations"),
    ("Conductor", None, "fires, polls, retries, mirrors to Spaces",
     "one dropped webhook silently killing the run"),
    ("QC Engineer", None, "ffmpeg measurement: LUFS, true peak, silence, duration",
     "shipping a track that clips, ends mid-bar, or is dead air"),
    ("Producer", "producer", "three independent scoring passes, then slot selection",
     "shipping the first five instead of the best five"),
    ("Packager", None, "master, art, tags, .lrc, delivery",
     "a bucket of untitled WAVs, and the 15-day retention clock"),
    ("Archivist", "retro", "the learning record; weekly retro proposes codex edits",
     "day 30 being exactly as good as day 1"),
]



# ── the files behind a song ──────────────────────────────────────────────────
# Basenames are fixed by the ship loop: every file in the clip folder is
# uploaded under its own name (pipeline.py), source.wav deliberately excluded.
FILE_LABELS = [("master.mp3", "MP3"), ("master.wav", "WAV"), ("cover.jpg", "Cover"),
               ("lyrics.txt", "Lyrics"), ("lyrics.lrc", "LRC"), ("meta.json", "Meta")]

# The names the ship loop writes for every clip it delivers. Cover art is
# skippable and can fail, so it is the one basename nothing may assume.
ALWAYS_SHIPPED = ("master.mp3", "master.wav", "lyrics.txt", "lyrics.lrc", "meta.json")

# What a row says when there is nothing behind it, which is two different facts.
GONE = ("no audio kept", "no files kept")
OFFSITE = ("not served from here", "not served from here")


def _unexpired():
    """The read path in app.py 404s an expired row, so anything the console
    offers has to be filtered the same way or it starts advertising dead links
    in the window between a row expiring and the purge collecting it."""
    return (StoredFile.expires_at.is_(None)) | (StoredFile.expires_at > utcnow())


def _files_for(s, clip_ids: list[int]) -> dict[int, dict[str, str]]:
    """{clip_id: {basename: key}}. Column-only on purpose: StoredFile.data is an
    undeferred LargeBinary, so selecting the entity would pull every master.wav
    on the page into a 512 MB container."""
    if not clip_ids:
        return {}
    out: dict[int, dict[str, str]] = {}
    for clip_id, key in s.execute(
            select(StoredFile.clip_id, StoredFile.key)
            .where(StoredFile.clip_id.in_(clip_ids), _unexpired())).all():
        out.setdefault(clip_id, {})[key.rsplit("/", 1)[-1]] = key
    return out


def _ratings(s, clip_ids: list[int]) -> dict[int, int]:
    if not clip_ids:
        return {}
    return {cid: r for cid, r in s.execute(
        select(Outcome.clip_id, Outcome.rating)
        .where(Outcome.clip_id.in_(clip_ids), Outcome.rating.isnot(None))).all()}


OFFSITE_NOTE = (
    '<div class="note"><span>where the audio is</span>'
    "The delivered files went to a store this console cannot read back — a "
    "local directory is a path on a disk, not a URL a browser will follow. "
    "AUDIO_STORE=database is what puts the bytes behind /files, and a player on "
    "this page.</div>")


def _elsewhere() -> tuple[Callable[[str], str] | None, tuple[str, str], str]:
    """How to reach a shipped clip whose bytes are not rows in this database.

    Three stores write the same layout and they are not equally readable from a
    browser. Under the database store there is nothing to reach: a clip with no
    row was collected by the purge, and the audio really is gone. Spaces can
    sign an ordinary HTTPS URL for any key without a round trip, so an operator
    on the storage the README describes gets a working player rather than a page
    of rows claiming nothing was kept. LocalStore hands out file:// paths, which
    a page served over HTTP cannot load at all — that one has to say so.

    Returns (sign, absent, note): a signer or None, the pair of phrases a row
    with nothing behind it prints, and a page-level explanation or "".
    """
    cfg = settings()
    if cfg.audio_store == "database":
        return None, GONE, ""
    from ..storage import open_store   # boto3, and only this branch needs it
    try:
        store = open_store()
        # Probed rather than type-checked: the only thing that matters is
        # whether the address it hands out is one a browser will follow.
        probe = store.signed_url("probe")
    except Exception as exc:
        log.warning("cannot address the delivery store from the console: %s", exc)
        probe = ""
    if probe.startswith(("http://", "https://")):
        return store.signed_url, GONE, ""
    return None, OFFSITE, OFFSITE_NOTE


def _hrefs(s, clips: list[Clip], sign: Callable | None) -> dict[int, dict[str, str]]:
    """{clip_id: {basename: href}}, unescaped — a signed URL carries a query.

    A row in stored_files wins wherever there is one: it is checked, and it
    survives a change of backend. A clip with none is addressed in the store the
    run shipped to, and only for the names that store always holds. Signing is
    local arithmetic but not free, so it is done for those clips and no others.
    """
    out = {cid: {name: f"/files/{key}" for name, key in got.items()}
           for cid, got in _files_for(s, [c.id for c in clips]).items()}
    if sign is None:
        return out
    for c in clips:
        if c.id not in out and c.spaces_key:
            out[c.id] = {n: sign(f"{c.spaces_key}/{n}") for n in ALWAYS_SHIPPED}
    return out


def _day_pages(s, spaces_keys: list[str],
               sign: Callable | None) -> dict[str, str]:
    """{day folder: an href to its index.html}.

    The folder comes from the clip's own key rather than from SPACES_PREFIX and
    the date, so a day that shipped under a different prefix still resolves. A
    row is proof the file is there; without one the page is only reachable if
    the store the run shipped to can be addressed at all, which is what ``sign``
    answers — an unsigned guess is a 404 under two of the three stores.
    """
    folders = {k.rsplit("/", 1)[0] for k in spaces_keys if k and "/" in k}
    if not folders:
        return {}
    candidates = {f"{f}/index.html": f for f in folders}
    found = s.execute(select(StoredFile.key)
                      .where(StoredFile.key.in_(list(candidates)),
                             _unexpired())).scalars().all()
    out = {candidates[k]: f"/files/{k}" for k in found}
    if sign is not None:
        for key, folder in candidates.items():
            out.setdefault(folder, sign(key))
    return out


def _player(files: dict[str, str], absent: str, *, preload: str) -> str:
    """The MP3, never the WAV — same music, a seventh of the bytes.

    ``preload`` is the caller's because the right answer differs by page, and
    templates.py settled why: "metadata" costs a few KB per track and is what
    makes the transport show a real duration instead of 0:00 / 0:00, which is
    what a page you glance at needs. A listing bounded at one day of songs can
    afford it. The full catalogue cannot — that is three hundred sources opened
    before anyone presses play.
    """
    href = files.get("master.mp3")
    if not href:
        return f'<span class="mini">{esc(absent)}</span>'
    return f'<audio controls preload="{preload}" src="{esc(href)}"></audio>'


def _links(files: dict[str, str], absent: str) -> str:
    parts = [f'<a href="{esc(href)}" download>{label}</a>'
             for name, label in FILE_LABELS if (href := files.get(name))]
    if not parts:
        # A shipped Clip outliving its bytes is normal, not an error: the purge
        # reads expires_at and never touches clips.
        return f'<span class="mini">{esc(absent)}</span>'
    return f'<span class="mini">{" · ".join(parts)}</span>'


def _rate(clip_id: int, rating: int | None, back: str) -> str:
    """A real form, so the rating works with the enhancement script switched off.

    ``back`` is where the redirect lands afterwards; app.py refuses anything
    that is not a path on this origin.
    """
    buttons = "".join(
        f'<button type="submit" name="rating" value="{n}" '
        f'aria-pressed="{"true" if rating == n else "false"}" '
        f'aria-label="Rate {n} out of 10">{n}</button>' for n in range(1, 11))
    done = f"rated {int(rating)}/10" if rating else ""
    return (f'<form class="rate" method="post" action="/console/rate" '
            f'data-rate="{int(clip_id)}">'
            f'<input type="hidden" name="clip_id" value="{int(clip_id)}">'
            f'<input type="hidden" name="back" value="{esc(back)}">'
            f'{buttons}<span class="done" role="status">{esc(done)}</span></form>')


def _song_meta(c: Clip, persona: str | None) -> str:
    bits = [b for b in (c.slot_type.value,
                        f"{c.bpm_target} BPM" if c.bpm_target else None,
                        c.musical_key, dur(c.duration_s), persona) if b]
    return esc(" · ".join(str(b) for b in bits))


def _song_card(c: Clip, persona: str | None, files: dict[str, str],
               absent: tuple[str, str], rating: int | None, back: str) -> str:
    no_audio, no_files = absent
    return (f'<div class="song" id="clip{int(c.id)}">'
            f'<div class="ttl">{esc(c.title or "untitled")}</div>'
            f'<div class="mini">{_song_meta(c, persona)}</div>'
            f'{_player(files, no_audio, preload="metadata")}'
            f'<div>{_links(files, no_files)}</div>'
            f'{_rate(c.id, rating, back)}</div>')


def _delivered_link(href: str | None) -> str:
    """The page the pipeline wrote into the day's folder — five players and the
    rating control, exactly as delivered. Nothing linked to it before."""
    if not href:
        return ""
    return (f'<p class="sub"><a href="{esc(href)}">the day page '
            f'as delivered</a></p>')


# ── overview ─────────────────────────────────────────────────────────────────
def overview() -> str:
    st = learning_status()
    cfg = settings()
    with session_scope() as s:
        runs = s.execute(select(Run).order_by(Run.run_date.desc()).limit(8)).scalars().all()
        rows = [_run_row(s, r) for r in runs]
        spent = s.execute(
            select(func.sum(Run.credits_start - Run.credits_end))
            .where(Run.credits_end.isnot(None))).scalar() or 0
        today = _todays_songs(s)

    body = [
        "<h1>Overview</h1>",
        f'<p class="sub">Five finished songs a day, unattended. '
        f'{esc(cfg.full_slots)} full-length and {esc(cfg.short_slots)} short-form, '
        f'chosen from {esc(cfg.total_briefs * 2)} candidates.</p>',
        stats(("runs", st["runs"]), ("clips", st["clips"]), ("shipped", st["shipped"]),
              ("rated", st["rated"]), ("credits", spent or "—")),
        f'<div class="note"><span>Learning signal</span>{esc(st["signal"])}</div>',
        today,
        "<h2>Brains</h2>", _brain_table(),
        "<h2>Recent runs</h2>",
        table(["Date", "Phase", "Clips", "Shipped", "Cut", "Rated", "Credits", ""],
              rows, empty="no runs yet — `dailyfive run` starts one",
              num_cols={2, 3, 4, 5, 6}),
    ]
    return page("Overview", "".join(body), "/", script=RATE_JS)


def _todays_songs(s) -> str:
    """The set most recently shipped, playable and ratable without leaving here.

    The daily habit is open the console, hear today's five, rate them, so this
    sits above the runs table rather than inside it — five statements for one
    date, not a lookup hung off every row of a list that already does one query
    per run.

    The latest day that *shipped*, not today's date: a run that failed or has
    not finished would otherwise render an empty block where the songs go.
    """
    latest = s.execute(select(func.max(Run.run_date)).select_from(Run)
                       .join(Clip, Clip.run_id == Run.id)
                       .where(Clip.shipped.is_(True))).scalar()
    if latest is None:
        return ""
    rows = s.execute(
        select(Clip, Brief.persona_name)
        .join(Run, Clip.run_id == Run.id)
        .join(Brief, Clip.brief_id == Brief.id)
        .where(Run.run_date == latest, Clip.shipped.is_(True))
        .order_by(Clip.rank, Clip.id)).all()
    if not rows:
        return ""

    clips = [c for c, _ in rows]
    sign, absent, _note = _elsewhere()
    files = _hrefs(s, clips, sign)
    ratings = _ratings(s, [c.id for c in clips])
    day_pages = _day_pages(s, [c.spaces_key for c in clips], sign)
    folders = [c.spaces_key.rsplit("/", 1)[0] for c in clips
               if c.spaces_key and "/" in c.spaces_key]
    index_href = next((day_pages[f] for f in folders if f in day_pages), None)

    day = latest.isoformat()
    cards = "".join(
        _song_card(c, persona, files.get(c.id, {}), absent, ratings.get(c.id), "/")
        for c, persona in rows)
    return "".join([
        f'<h2>{esc(day)} — the latest set <a href="/runs/{esc(day)}">run</a>'
        + (f' <a href="{esc(index_href)}">as delivered</a>' if index_href else "")
        + "</h2>",
        f'<div class="grid2">{cards}</div>',
    ])


def _brain_table() -> str:
    cfg = settings()
    roster = llm.roster()
    summary = ledger.role_summary(30)
    needed = {"anthropic": "anthropic_api_key", "minimax": "minimax_api_key",
              "openai-compatible": "llm_api_key"}
    rows = []
    for role, brain in roster.items():
        st = summary.get(role, {})
        attr = needed.get(brain.provider)
        if brain.provider == "unconfigured":
            status = pill("misconfigured", "bad")
        elif attr and not getattr(cfg, attr, ""):
            status = pill("no key", "bad")
        else:
            status = pill("ready", "ok")
        rows.append([
            f"<b>{esc(role)}</b>",
            f'<span class="mini">{esc(brain.provider)}</span>',
            esc(brain.model),
            status,
            f'<span class="num">{st.get("calls", 0)}</span>',
            f'<span class="mini">{ms(st.get("ms", 0))}</span>',
            (pill(f'{st["failures"]} failed', "bad") if st.get("failures")
             else '<span class="mini">—</span>'),
        ])
    return table(["Role", "Provider", "Model", "Key", "Calls 30d", "Time", "Failures"],
                 rows, num_cols={4})


def _run_row(s, r: Run) -> list[str]:
    clips = s.execute(select(Clip).where(Clip.run_id == r.id)).scalars().all()
    shipped = sum(1 for c in clips if c.shipped)
    cut = sum(1 for c in clips if c.qc_verdict == "fail")
    rated = sum(1 for c in clips if c.outcome and c.outcome.rating is not None)
    kind = {"shipped": "ok", "failed": "bad"}.get(r.phase.value, "hot")
    if r.phase.value == "shipped" and (r.notes or {}).get("shortfall"):
        kind = "hot"          # green would claim a clean day it did not have
    return [
        f'<a class="q" href="/runs/{r.run_date.isoformat()}">{esc(r.run_date)}</a>',
        pill(r.phase.value, kind),
        f'<span class="num">{len(clips)}</span>',
        (f'<span class="num">{shipped}</span>'
         + (' <span class="pill p-bad">short</span>'
            if (r.notes or {}).get("shortfall") else "")),
        f'<span class="num">{cut}</span>',
        f'<span class="num">{rated}</span>',
        f'<span class="num">{r.credits_spent if r.credits_spent is not None else "—"}</span>',
        f'<span class="mini">{ago(r.created_at)}</span>',
    ]


# ── runs list ────────────────────────────────────────────────────────────────
def runs_list() -> str:
    with session_scope() as s:
        runs = s.execute(select(Run).order_by(Run.run_date.desc()).limit(120)).scalars().all()
        rows = [_run_row(s, r) for r in runs]
    return page("Runs", "".join([
        "<h1>Runs</h1>",
        '<p class="sub">Every run this studio has attempted, newest first. '
        'Open one to see the themes, the briefs, every candidate and why each was '
        'kept or cut.</p>',
        table(["Date", "Phase", "Clips", "Shipped", "Cut", "Rated", "Credits", ""],
              rows, empty="no runs yet", num_cols={2, 3, 4, 5, 6}),
    ]), "/runs")


# ── run detail ───────────────────────────────────────────────────────────────
def run_detail(run_date: date) -> str | None:
    with session_scope() as s:
        run = s.execute(select(Run).where(Run.run_date == run_date)).scalar_one_or_none()
        if run is None:
            return None
        signals = s.execute(select(Signal).where(Signal.run_id == run.id)
                            .order_by(Signal.rank)).scalars().all()
        briefs = s.execute(select(Brief).where(Brief.run_id == run.id)
                           .order_by(Brief.slot_type, Brief.idx)).scalars().all()
        jobs = s.execute(select(Job).where(Job.run_id == run.id)
                         .order_by(Job.id)).scalars().all()
        clips = s.execute(select(Clip).where(Clip.run_id == run.id)
                          .order_by(Clip.shipped.desc(), Clip.rank,
                                    Clip.id)).scalars().all()
        decision = s.execute(select(Decision).where(
            Decision.run_id == run.id)).scalar_one_or_none()

        sig_rows = [[
            f'<span class="num">{sg.rank}</span>',
            f'<b>{esc(sg.theme)}</b>',
            esc(sg.sentiment),
            pill(sg.lead, {"leading": "cool", "moderate": "hot"}.get(sg.lead, "dim")),
            f'<span class="num">{sg.confidence:.2f}</span>',
            f'<span class="mini">{esc(", ".join(sg.sources or []))}</span>',
            f'<td class="w">{esc(sg.evidence)}</td>'.replace("<td class=\"w\">", "").replace("</td>", ""),
        ] for sg in signals]

        brief_rows = []
        for b in briefs:
            cl = b.clearance or {}
            if b.dropped_reason:
                state = pill("dropped", "bad")
            elif cl.get("verdict") == "rewrite":
                state = pill("rewritten", "hot")
            elif b.lyrics:
                state = pill("cleared", "ok")
            else:
                state = pill("pending", "dim")
            brief_rows.append([
                f'<b>{esc(b.title)}</b><br><span class="mini">{esc(b.theme or "")[:90]}</span>',
                pill(b.slot_type.value, "cool" if b.slot_type.value == "short" else "dim"),
                esc(b.persona_name or "—"),
                f'<span class="mini">{esc(b.bpm or "?")} BPM · {esc(b.musical_key or "?")}</span>',
                f'<span class="mini">{esc((b.style_string or "")[:120])}</span>',
                state,
                f'<span class="mini">{esc(b.dropped_reason or "")[:120]}</span>',
            ])

        job_rows = [[
            f'<span class="mini">{esc(j.task_id or "—")}</span>',
            esc(s.get(Brief, j.brief_id).title if s.get(Brief, j.brief_id) else "?"),
            pill(j.state.value, _job_kind(j.state.value)),
            f'<span class="num">{j.attempts}</span>',
            f'<span class="num">{j.callbacks_seen}</span>',
            f'<span class="num">{j.polls}</span>',
            f'<span class="mini">{esc((j.last_error or "")[:110])}</span>',
        ] for j in jobs]

        clip_rows = []
        for c in clips:
            rating = c.outcome.rating if c.outcome else None
            if c.shipped:
                verdict = pill(f"shipped #{c.rank}", "ok")
            elif c.qc_verdict == "fail":
                verdict = pill("QC cut", "bad")
            else:
                verdict = pill("held", "dim")
            q = c.qc or {}
            clip_rows.append([
                f'<b>{esc(c.title or "untitled")}</b>',
                pill(c.slot_type.value, "cool" if c.slot_type.value == "short" else "dim"),
                verdict,
                f'<span class="num">{dur(c.duration_s)}</span>',
                f'<span class="mini">{_qc_cell(q)}</span>',
                _score_cell(c),
                f'<span class="num">{rating if rating else "—"}</span>',
                f'<span class="mini">{esc((c.qc_reason or c.reject_reason or "")[:130])}</span>',
            ])

        shipped_clips = [c for c in clips if c.shipped]
        sign, absent, _note = _elsewhere()
        no_audio, no_files = absent
        clip_files = _hrefs(s, shipped_clips, sign)
        day_pages = _day_pages(s, [c.spaces_key for c in shipped_clips], sign)
        back = f"/runs/{run_date.isoformat()}"
        files = [[
            f'<b id="clip{int(c.id)}">{esc(c.title)}</b><br>'
            f'<span class="mini">{esc(c.spaces_key or "not uploaded")}</span>',
            _player(clip_files.get(c.id, {}), no_audio, preload="metadata"),
            _links(clip_files.get(c.id, {}), no_files),
            pill("wav ready", "ok") if c.wav_url else pill("mp3 only", "dim"),
            # c.outcome is already in the identity map from the clip_rows loop.
            _rate(c.id, c.outcome.rating if c.outcome else None, back),
        ] for c in shipped_clips]
        folders = [c.spaces_key.rsplit("/", 1)[0] for c in shipped_clips
                   if c.spaces_key and "/" in c.spaces_key]
        delivered = _delivered_link(next(
            (day_pages[f] for f in folders if f in day_pages), None))

    calls = ledger.for_run(run.id)
    call_rows = [[
        esc(c["role"]),
        f'<span class="mini">{esc(c["label"])}</span>',
        f'<span class="mini">{esc(c["provider"] or "—")}:{esc(c["model"] or "—")}</span>',
        pill("ok", "ok") if c["ok"] else pill("failed", "bad"),
        f'<span class="num">{ms(c["ms"])}</span>',
        f'<span class="num">{c["chars_in"]:,}</span>',
        f'<span class="num">{c["chars_out"]:,}</span>',
        f'<span class="mini">{esc((c["error"] or "")[:110])}</span>',
    ] for c in calls]

    body = [
        f"<h1>{esc(run_date)}</h1>",
        f'<p class="sub">{pill(run.phase.value, _run_kind(run.phase))} '
        f'codex v{esc(run.codex_version or "?")} · started {ago(run.created_at)}'
        + (f' · finished {ago(run.finished_at)}' if run.finished_at else "")
        + (f' · <span style="color:var(--bad)">{esc(run.error[:160])}</span>'
           if run.error else "") + "</p>",
        stats(("clips", len(clips)),
              ("shipped", sum(1 for c in clips if c.shipped)),
              ("QC cut", sum(1 for c in clips if c.qc_verdict == "fail")),
              ("brain calls", len(calls)),
              ("credits", run.credits_spent if run.credits_spent is not None else "—")),
        _phase_flow(run.phase),
        _shortfall_note(run),
        "<h2>What the Scout found</h2>",
        table(["#", "Theme", "Feeling", "Lead", "Conf.", "Sources", "Evidence"],
              sig_rows, empty="no signals recorded", num_cols={0, 4}),
        "<h2>Briefs</h2>",
        table(["Title", "Slot", "Persona", "Musical", "Style string", "State", "Note"],
              brief_rows, empty="no briefs"),
        "<h2>Generation jobs</h2>",
        table(["Task", "Brief", "State", "Tries", "Callbacks", "Polls", "Error"],
              job_rows, empty="nothing submitted", num_cols={3, 4, 5}),
        "<h2>Every candidate</h2>",
        f'<p class="sub">All {len(clips)} — the ones that shipped and the ones that '
        f'did not, with the measurement or the reasoning that decided it.</p>',
        table(["Title", "Slot", "Outcome", "Length", "QC", "Scores", "Rating", "Why"],
              clip_rows, empty="no clips", num_cols={3, 6}),
    ]
    if decision and decision.rationale:
        body += ["<h2>Producer's reasoning</h2>",
                 f'<div class="note"><span>the day as a set</span>'
                 f'{esc(decision.rationale)}</div>']
    body += [
        "<h2>Brain calls</h2>",
        f'<p class="sub">Every model call this run made, in order.</p>',
        table(["Role", "Step", "Brain", "", "Time", "In", "Out", "Error"],
              call_rows, empty="no brain calls recorded", num_cols={4, 5, 6}),
        "<h2>Delivered</h2>",
        delivered,
        table(["Song", "Listen", "Files", "Master", "Rate"], files,
              empty="nothing delivered yet"),
    ]
    return page(str(run_date), "".join(body), "/runs", script=RATE_JS)


def _shortfall_note(run: Run) -> str:
    """A run that shipped four against a contract of five must not read as clean."""
    sf = (run.notes or {}).get("shortfall")
    if not sf:
        return ""
    causes = "".join(f"<div>{esc(c)}</div>" for c in sf.get("causes", []))
    return (f'<div class="note" style="border-left-color:var(--bad)">'
            f'<span>Short of contract</span>{causes}</div>')


def _qc_cell(q: dict) -> str:
    bits = []
    if q.get("lufs_i") is not None:
        bits.append(f'{q["lufs_i"]:.1f} LUFS')
    if q.get("true_peak_db") is not None:
        bits.append(f'{q["true_peak_db"]:+.1f} dBTP')
    if q.get("silence_ratio"):
        bits.append(f'{q["silence_ratio"] * 100:.0f}% silent')
    return esc(" · ".join(bits)) or "—"


def _score_cell(c: Clip) -> str:
    if c.score_total is None:
        return '<span class="mini">—</span>'
    return (f'<b>{c.score_total:.2f}</b>'
            f'<span class="mini"> h{c.score_hook or 0:.0f} '
            f'm{c.score_mix or 0:.0f} t{c.score_trend or 0:.0f}</span>'
            + bar(c.score_total, 10))


def _job_kind(state: str) -> str:
    return {"mirrored": "ok", "success": "ok", "failed": "bad",
            "abandoned": "bad"}.get(state, "hot")


def _run_kind(phase: RunPhase) -> str:
    return {"shipped": "ok", "failed": "bad"}.get(phase.value, "hot")


def _phase_flow(current: RunPhase) -> str:
    try:
        reached = PHASES.index(current) if current in PHASES else -1
    except ValueError:
        reached = -1
    cells = []
    for i, ph in enumerate(PHASES):
        cls = "done" if i <= reached else ""
        if i == reached:
            cls = "now" if current is not RunPhase.SHIPPED else "done"
        cells.append(f'<div class="{cls}"><b>{i + 1}</b>{esc(PHASE_LABEL[ph])}</div>')
    return f'<div class="flow">{"".join(cells)}</div>'


# ── agents ───────────────────────────────────────────────────────────────────
def agents_page() -> str:
    roster = llm.roster()
    summary = ledger.role_summary(30)
    rows = []
    for name, role, does, prevents in ROSTER:
        if role is None:
            brain = f'{pill("no LLM", "cool")} <span class="mini">plain code</span>'
            activity = '<span class="mini">deterministic</span>'
            fails = '<span class="mini">—</span>'
        else:
            b = roster.get(role)
            brain = (f'<span class="mini">{esc(b.provider)}</span><br>{esc(b.model)}'
                     if b else "—")
            st = summary.get(role, {})
            activity = (f'<span class="num">{st.get("calls", 0)}</span>'
                        f'<span class="mini"> calls · {ms(st.get("ms", 0))}</span>')
            fails = (pill(f'{st["failures"]}', "bad") if st.get("failures")
                     else '<span class="mini">0</span>')
        rows.append([f"<b>{esc(name)}</b>", brain,
                     f'<span class="mini">{esc(does)}</span>', activity, fails,
                     f'<span class="mini">{esc(prevents)}</span>'])

    no_brain = sum(1 for _, role, _, _ in ROSTER if role is None)
    recent = ledger.recent(60)
    recent_rows = [[
        f'<span class="mini">{ago(c["created_at"])}</span>',
        esc(c["role"]),
        f'<span class="mini">{esc(c["label"])}</span>',
        f'<span class="mini">{esc(c["provider"] or "—")}:{esc(c["model"] or "—")}</span>',
        pill("ok", "ok") if c["ok"] else pill("failed", "bad"),
        f'<span class="num">{ms(c["ms"])}</span>',
        f'<span class="mini">{esc((c["error"] or "")[:100])}</span>',
    ] for c in recent]

    return page("Agents", "".join([
        "<h1>Agents</h1>",
        f'<p class="sub">Eleven roles. <b>{no_brain} of them have no language model '
        f'at all</b> — that is what makes this cheap enough to run every day and '
        f'reliable enough to run unattended. Audio quality is decided by '
        f'measurement, never judgment.</p>',
        table(["Agent", "Brain", "Does", "30 days", "Fails", "Prevents"], rows),
        "<h2>Recent brain calls</h2>",
        table(["When", "Role", "Step", "Brain", "", "Time", "Error"], recent_rows,
              empty="no calls recorded yet"),
    ]), "/agents")


# ── codex ────────────────────────────────────────────────────────────────────
def codex_page() -> str:
    cx = current_codex()
    with session_scope() as s:
        history = s.execute(select(CodexVersion)
                            .order_by(CodexVersion.version.desc())
                            .limit(20)).scalars().all()
        hist_rows = [[
            f'<b>v{v.version}</b>',
            f'<span class="mini">{ago(v.created_at)}</span>',
            f'<span class="mini">{esc(v.diff or "—")}</span>',
            f'<span class="mini">{esc(v.rationale or "")[:160]}</span>',
        ] for v in history]

    persona_rows = [[
        f'<b>{esc(p.get("name"))}</b>',
        esc(p.get("vocal_gender") or "—"),
        (pill("registered", "ok") if p.get("persona_id") else pill("not created", "bad")),
        f'<span class="mini">{esc(p.get("persona_id") or "run `dailyfive personas bootstrap`")}</span>',
        f'<span class="mini">{esc(p.get("territory", ""))}</span>',
    ] for p in cx.personas]

    learned = cx.body.get("learned", {})
    styles = learned.get("style_scores") or {}
    top = sorted(styles.items(), key=lambda kv: -kv[1])[:12]
    learn_rows = [[esc(k), f'<span class="num">{v:.2f}</span>', bar(v, 10)]
                  for k, v in top]

    stats_now = aggregate(60)
    return page("Codex", "".join([
        f"<h1>Style Codex v{cx.version}</h1>",
        '<p class="sub">The Music Director\'s working document: production '
        'specifications precise enough that a generation can be checked against '
        'them afterwards. Versioned, never edited in place, so a regression traces '
        'back to the edit that caused it.</p>',
        stats(("version", cx.version), ("personas", len(cx.personas)),
              ("registered", len(cx.personas_ready())),
              ("observations", stats_now["observations"]),
              ("avoid list", len(learned.get("avoid") or []))),
        "<h2>Persona cast</h2>",
        table(["Name", "Voice", "State", "Persona ID", "Territory"], persona_rows,
              empty="no personas defined"),
        "<h2>What it has learned</h2>",
        (f'<p class="sub">Nothing yet — {stats_now["observations"]} observations '
         f'recorded, and an average needs at least four behind it before it is '
         f'reported as a trend.</p>' if not top else
         '<p class="sub">Style descriptors scored by outcome. Your ratings win '
         'outright where they exist; the Producer\'s score stands in, damped, where '
         'they do not.</p>'),
        table(["Style descriptor", "Score", ""], learn_rows,
              empty="no descriptor has enough observations behind it yet",
              num_cols={1}),
        *([f'<h2>Avoid list</h2><div class="note"><span>observed to fail</span>'
           f'{esc(", ".join(learned["avoid"]))}</div>'] if learned.get("avoid") else []),
        "<h2>Tempo bands and forms</h2>",
        jsonblock({"tempo_bands": cx.body.get("tempo_bands"),
                   "hook_placement": cx.body.get("hook_placement"),
                   "mix_targets": cx.body.get("mix_targets"),
                   "instrumentation_palettes": cx.body.get("instrumentation_palettes")}),
        "<h2>Version history</h2>",
        table(["Version", "When", "Change", "Rationale"], hist_rows,
              empty="only the seed version exists"),
    ]), "/codex")


# ── files ────────────────────────────────────────────────────────────────────
def files_page() -> str:
    cfg = settings()
    with session_scope() as s:
        rows_db = s.execute(
            select(Clip, Run.run_date, Brief.persona_name)
            .join(Run, Clip.run_id == Run.id)
            .join(Brief, Clip.brief_id == Brief.id)
            .where(Clip.shipped.is_(True))
            .order_by(Run.run_date.desc(), Clip.rank).limit(300)).all()
        clips = [c for c, _, _ in rows_db]
        sign, absent, store_note = _elsewhere()
        no_audio, no_files = absent
        files = _hrefs(s, clips, sign)
        ratings = _ratings(s, [c.id for c in clips])
        day_pages = _day_pages(s, [c.spaces_key for c in clips], sign)

    by_day: dict[str, list] = {}
    for clip, run_date, persona in rows_db:
        by_day.setdefault(run_date.isoformat(), []).append((clip, persona))

    blocks = []
    for day, entries in by_day.items():
        rows = []
        for c, persona in entries:
            mine = files.get(c.id, {})
            note = [c.spaces_key or "not uploaded"]
            if ratings.get(c.id):
                note.append(f"rated {ratings[c.id]}/10")
            rows.append([
                f'<b id="clip{int(c.id)}">{esc(c.title)}</b><br>'
                f'<span class="mini">{esc(" · ".join(note))}</span>',
                pill(c.slot_type.value, "cool" if c.slot_type.value == "short" else "dim"),
                esc(persona or "—"),
                f'<span class="num">{dur(c.duration_s)}</span>',
                _player(mine, no_audio, preload="none"),
                _links(mine, no_files),
            ])
        folder = next((c.spaces_key.rsplit("/", 1)[0] for c, _ in entries
                       if c.spaces_key and "/" in c.spaces_key), None)
        index_href = day_pages.get(folder)
        heading = (f'<h2>{esc(day)} <a href="/runs/{esc(day)}">run</a>'
                   + (f' <a href="{esc(index_href)}">as delivered</a>'
                      if index_href else "") + "</h2>")
        blocks += [heading,
                   table(["Song", "Slot", "Persona", "Length", "Listen", "Files"], rows)]

    prefix = f"{cfg.spaces_bucket or '<bucket>'}/{cfg.spaces_prefix}"
    return page("Files", "".join([
        "<h1>Delivered files</h1>",
        '<p class="sub">Everything this studio has shipped'
        + ("." if store_note else ", and this is where you listen to it.")
        + ' One immutable dated folder per run, mirrored the moment the '
        'bytes existed — Suno deletes its own copies after 15 days. Rating happens '
        'on the day\'s run page, where the set is small enough to judge.</p>',
        store_note,
        f'<pre>spaces://{esc(prefix)}/YYYY-MM-DD/\n'
        f'├─ manifest.json\n├─ index.html          '
        f'<i>the rating page</i>\n'
        f'├─ 01_slug/  master.wav · master.mp3 · cover.jpg · lyrics.txt · '
        f'lyrics.lrc · meta.json\n└─ _rejected/rejects.json</pre>',
        *(blocks or ['<div class="empty">nothing delivered yet</div>']),
    ]), "/files")
