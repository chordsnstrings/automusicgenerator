"""Console page bodies. Each reads the database and returns HTML."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta

from sqlalchemy import func, select

from .. import genres, ledger, llm
from ..archivist import aggregate, learning_status
from ..codex import current as current_codex
from ..codex import is_negation
from ..codex import scored as codex_scored
from ..config import settings
from ..db import session_scope
from ..models import (AgentCall, Brief, Clip, CodexVersion, Decision, Job,
                      Outcome, Publication, Run, RunPhase, Signal, StoredFile,
                      utcnow)
from .console import (RATE_JS, ago, bar, dur, esc, jsonblock, ms, page, pill,
                      stats, table)

log = logging.getLogger(__name__)

PHASES = [RunPhase.SENSED, RunPhase.BRIEFED, RunPhase.WRITTEN, RunPhase.SUBMITTED,
          RunPhase.RENDERED, RunPhase.JUDGED, RunPhase.SHIPPED]
PHASE_LABEL = {RunPhase.SENSED: "sense", RunPhase.BRIEFED: "brief",
               RunPhase.WRITTEN: "write", RunPhase.SUBMITTED: "submit",
               RunPhase.RENDERED: "render", RunPhase.JUDGED: "judge",
               RunPhase.SHIPPED: "ship"}

# The full roster, including the entries that deliberately have no brain. How
# many that is is computed where it is printed, not written here: the last time
# it was a literal it said four while the list held five.
ROSTER = [
    ("Scout", "scout", "trend + sentiment signals from seven free feeds",
     "making music for a mood that peaked three weeks ago"),
    # No language model, and the seat on this page is the honest way to say so.
    # Scoring what worked is a GROUP BY with a sample floor; allocating the day
    # is UCB1 with a cap, and a deterministic slate is the only kind the console
    # can explain — "country got 3 because its mean is 7.4 over 12 rated songs
    # and the explore floor is 2 of 7" is a sentence a model asked to allocate
    # could not be held to twice. The one part of the job that is judgement —
    # deciding that a spec is country > country-soul — belongs to the Music
    # Director, who is already reading the specification when the decision is
    # made, and costs no extra call.
    ("Genre Director", None, "closed vocabulary; allocates the day's slate from your ratings",
     "learning nothing about genre because every style string is unique prose"),
    ("Music Director", "director", "owns the Style Codex; themes become checkable spec",
     "prompts made of adjectives instead of specifications"),
    ("A&R", "anr", "one brief per spec, personas assigned, diversity enforced",
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
               ("short.mp4", "Short"), ("lyric.mp4", "Lyric video"),
               ("lyrics.txt", "Lyrics"), ("lyrics.lrc", "LRC"), ("meta.json", "Meta")]

# On a same-origin link the download attribute forces a save whatever the
# Content-Type says, so it is the attribute and not the content type that decides
# what a click does. Audio is worth saving — nobody wants a browser to navigate
# to twelve megabytes of MP3 — while the three text artifacts are things you want
# to read, and now can, since they are served as types a browser renders inline.
# The two videos are deliberately NOT here. Audio is worth saving and text is
# worth reading, but a video is worth watching — forcing a save on a click means
# finding the file and opening it in something else to answer "did that cut land
# on the beat", which is the only question anybody opens it to ask.
DOWNLOADABLE = {"master.mp3", "master.wav", "cover.jpg"}

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
    parts = [f'<a href="{esc(href)}"{" download" if name in DOWNLOADABLE else ""}>'
             f'{label}</a>'
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
    # A second submit button in the same form, because a form cannot issue
    # DELETE and the no-JavaScript path has to keep working. Rendered only where
    # there is something to clear — offering to undo an unrated song is an
    # invitation to wonder what it would do. RATE_JS creates and removes it on
    # the same rule between reloads, which is the half that matters: the second
    # after a mis-tap is the second somebody wants an undo, and there is no
    # reload in it.
    clear = ('<button type="submit" name="clear" value="1" class="clear">clear</button>'
             if rating is not None else "")
    return (f'<form class="rate" method="post" action="/console/rate" '
            f'data-rate="{int(clip_id)}">'
            f'<input type="hidden" name="clip_id" value="{int(clip_id)}">'
            f'<input type="hidden" name="back" value="{esc(back)}">'
            f'{buttons}{clear}'
            f'<span class="done" role="status">{esc(done)}</span></form>')


def _song_meta(c: Clip, persona: str | None) -> str:
    bits = [b for b in (c.slot_type.value,
                        f"{c.bpm_target} BPM" if c.bpm_target else None,
                        c.musical_key, dur(c.duration_s), persona) if b]
    return esc(" · ".join(str(b) for b in bits))


def _reach(pubs: list[dict]) -> str:
    """Where this song was posted and what happened, or nothing at all.

    Nothing at all is the honest render for an unpublished song. An empty
    "0 views" would say the audience saw it and did not care, which is a
    different and much worse fact than not having posted it yet.
    """
    if not pubs:
        return ""
    parts = []
    for p in pubs:
        name = p["platform"].replace("youtube", "YouTube").replace("tiktok", "TikTok")
        if p["status"] != "live":
            parts.append(f'{esc(name)} <span class="mini">{esc(p["status"])}</span>')
            continue
        # Views only once they have been read back. A live posting whose metrics
        # have never been fetched is not a posting with no views.
        count = (f'{p["views"]:,} views' if p.get("views") is not None
                 else "posted")
        label = f'{esc(name)} — {esc(count)}'
        parts.append(f'<a href="{esc(p["url"])}">{label}</a>' if p.get("url") else label)
    return f'<div class="mini reach">{" · ".join(parts)}</div>'


def _song_card(c: Clip, persona: str | None, files: dict[str, str],
               absent: tuple[str, str], rating: int | None, back: str,
               pubs: list[dict] | None = None) -> str:
    no_audio, no_files = absent
    return (f'<div class="song" id="clip{int(c.id)}">'
            f'<div class="ttl">{esc(c.title or "untitled")}</div>'
            f'<div class="mini">{_song_meta(c, persona)}</div>'
            f'{_player(files, no_audio, preload="metadata")}'
            f'<div>{_links(files, no_files)}</div>'
            f'{_reach(pubs or [])}'
            f'{_rate(c.id, rating, back)}</div>')


def _publications(s, clip_ids: list[int]) -> dict[int, list[dict]]:
    """{clip_id: [publication, ...]} in one query rather than one per card."""
    if not clip_ids:
        return {}
    rows = s.execute(select(Publication)
                     .where(Publication.clip_id.in_(clip_ids))
                     .order_by(Publication.platform)).scalars().all()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r.clip_id, []).append({
            "platform": r.platform, "status": r.status, "url": r.url,
            "views": r.views, "likes": r.likes,
        })
    return out


def _delivered_link(href: str | None) -> str:
    """The page the pipeline wrote into the day's folder — five players and the
    rating control, exactly as delivered. Nothing linked to it before."""
    if not href:
        return ""
    return (f'<p class="sub"><a href="{esc(href)}">the day page '
            f'as delivered</a></p>')


# ── overview ─────────────────────────────────────────────────────────────────
def _shape_line(cfg) -> str:
    """The day's shape, read from the settings that produce it.

    This sentence is why the console went on announcing a split of full-length and
    short-form after the slot counts had stopped producing one: it was half prose,
    and prose does not change when a count does. Every number here is read, and a
    lane with no slots is not named at all — "0 short-form" is a category the
    console would be inventing.
    """
    lanes = [(n, label) for n, label in ((cfg.full_slots, "full-length"),
                                         (cfg.short_slots, "short-form")) if n]
    head = f"{cfg.total_slots} finished songs a day, unattended"
    cands = cfg.total_briefs * 2
    if len(lanes) == 1:
        return f"{head}, all {lanes[0][1]}, chosen from {cands} candidates."
    shape = " and ".join(f"{n} {label}" for n, label in lanes) or "no slots configured"
    return f"{head}. {shape}, chosen from {cands} candidates."


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
        f'<p class="sub">{esc(_shape_line(cfg))}</p>',
        stats(("runs", st["runs"]), ("clips", st["clips"]), ("shipped", st["shipped"]),
              ("rated", st["rated"]), ("credits", spent or "—")),
        f'<div class="note"><span>Learning signal</span>{esc(st["signal"])}</div>',
        f'<div class="note"><span>Genres</span>{esc(_genre_line())} '
        f'<a href="/genres">See the genre record</a>.</div>',
        today,
        "<h2>Brains</h2>", _brain_table(),
        "<h2>Recent runs</h2>",
        table(["Date", "Phase", "Clips", "Shipped", "Cut", "Rated", "Credits", ""],
              rows, empty="no runs yet — `dailyfive run` starts one",
              num_cols={2, 3, 4, 5, 6}),
    ]
    return page("Overview", "".join(body), "/", script=RATE_JS)


def _and(names: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`. Used wherever a tie has to be spelled out."""
    if len(names) < 2:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _genre_leaders(data: dict) -> tuple[list[str], float | None]:
    """The ranked families at the top, as a set, because a tie is an answer.

    Both the overview line and the /genres headline phrase from this and
    nothing else. They used to sort independently — one on ``(taste, f)`` and
    one on ``(-taste, f)`` — so two families on identical numbers made the two
    pages name opposite winners, and the overview links straight through to the
    page that reverses it. It is the same rule the chart split already follows:
    a tie broken silently hides the finding behind a sort order.
    """
    fams = data["families"]
    ranked = sorted(data["ranked"], key=lambda f: (-fams[f]["taste"], f))
    if not ranked:
        return [], None
    top = fams[ranked[0]]["taste"]
    return [f for f in ranked if fams[f]["taste"] == top], top


def _genre_line() -> str:
    """One computed sentence, because the daily habit lives on this page.

    Open the console, play the five, rate them — so the one thing a rating now
    feeds has to be visible from where the rating is given, rather than behind
    a nav link nobody has a reason to click.
    """
    data = genres.scores()
    fams, ranked = data["families"], data["ranked"]
    lead, top = _genre_leaders(data)
    if lead:
        if len(lead) == 1:
            return (f"{lead[0]} leads at {top:.1f} over "
                    f"{fams[lead[0]]['rated_n']} rated briefs, of {len(ranked)} "
                    f"ranked families.")
        return (f"{_and(lead)} are level at {top:.1f}, of {len(ranked)} ranked "
                f"families.")
    if data["rated_briefs"]:
        return (f"{data['rated_briefs']} rated briefs and no family has reached "
                f"{genres.GENRE_MIN_RATED}, so nothing is ranked yet.")
    if data["briefed_families"]:
        n = len(data["briefed_families"])
        return (f"{n} famil{'y' if n == 1 else 'ies'} briefed and none rated — "
                f"your rating is the only thing that ranks them.")
    # Not the same state as an empty studio, and saying so here is the whole
    # point: on the day this shipped the database held six briefs and twelve
    # clips, all from before genre was recorded. "Nothing briefed yet" would be
    # the one line on the console claiming the studio has made nothing.
    unlabelled = data["unlabelled_briefs"]
    if unlabelled:
        return (f"{unlabelled} brief{'' if unlabelled == 1 else 's'} carry no "
                f"genre — written before the studio recorded one, and keeping a "
                f"null rather than a guess. Nothing is ranked and nothing can "
                f"be until a run briefs a genre.")
    return (f"Nothing briefed yet. The vocabulary holds "
            f"{len(genres.FAMILIES)} families and {len(genres.SPECIFICS)} "
            f"specific genres.")


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
    pubs = _publications(s, [c.id for c in clips])
    day_pages = _day_pages(s, [c.spaces_key for c in clips], sign)
    folders = [c.spaces_key.rsplit("/", 1)[0] for c in clips
               if c.spaces_key and "/" in c.spaces_key]
    index_href = next((day_pages[f] for f in folders if f in day_pages), None)

    day = latest.isoformat()
    cards = "".join(
        _song_card(c, persona, files.get(c.id, {}), absent, ratings.get(c.id),
                   "/", pubs.get(c.id))
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
    # Appended rather than folded into the roster loop, which is LLM brains
    # only. Cover art is an image model and it is the one integration whose
    # absence a person currently learns about by reading a log — this is the
    # page where an operator reads configuration, so it belongs here rather
    # than in five ERROR lines a day.
    rows.append([
        "<b>cover art</b>",
        '<span class="mini">modelark</span>',
        "seedream",
        pill("ready", "ok") if cfg.ark_api_key else pill("no key", "bad"),
        '<span class="mini">—</span>',
        '<span class="mini">—</span>',
        '<span class="mini">—</span>',
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
        _art_note(run),
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


def _art_note(run: Run) -> str:
    """Why this day's folders hold no cover.jpg, on the page you open to ask.

    The pipeline stopped logging an ERROR per song for an integration nobody
    configured and wrote the reason onto the run instead. That was only half a
    trade: a WARNING in a log the console exists to save you from reading is
    not an answer to "why is there no artwork", and the alternative — noticing
    the missing link, then guessing your way to /agents — is the same walk the
    log was.

    Not styled as a failure. An unconfigured optional provider is a settled
    fact about the deployment, and the shortfall note's red rule is reserved
    for a day that owes songs.
    """
    art = (run.notes or {}).get("art")
    if not art or art.get("configured"):
        return ""
    reason = art.get("reason") or "not configured"
    return (f'<div class="note"><span>No cover art</span>'
            f'{esc(reason)} — the songs shipped without it.</div>')


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

    # Appended after the roster loop rather than folded into ROSTER, which is
    # the roster itself. Cover art is an image model, not one of them — but it
    # is the one integration whose absence a person could previously only learn
    # by reading five ERROR lines a day, and this is the page an operator opens
    # to find out what is wired up.
    rows.append([
        "<b>Cover art</b>",
        '<span class="mini">modelark</span><br>seedream',
        '<span class="mini">the 3000x3000 tile every distributor asks for</span>',
        ('<span class="mini">not configured</span>' if not settings().ark_api_key
         else '<span class="mini">—</span>'),
        (pill("no key", "bad") if not settings().ark_api_key else pill("ready", "ok")),
        '<span class="mini">nothing — every song ships without artwork</span>',
    ])

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
        f'<p class="sub">{len(ROSTER)} roles. <b>{no_brain} of them have no '
        f'language model at all</b> — that is what makes this cheap enough to run every day and '
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
    # Filtered here as well as in brief_context(), and for the same reason: the
    # live codex reached v3 with a learned table that was entirely "no synths"
    # 6.11 and "no pads" 6.11, and a version is never rewritten in place, so
    # those two rows are in the record forever. Rendering them under a heading
    # saying they were scored by outcome would make this page teach a reader
    # what the prompt was about to teach the Director. Dropped rows are counted
    # rather than silently hidden — a reader comparing this against the stored
    # body should be able to see where the difference went.
    scoreable = {k: v for k, v in styles.items() if not is_negation(k)}
    dropped = len(styles) - len(scoreable)
    top = sorted(scoreable.items(), key=lambda kv: -kv[1])[:12]
    learn_rows = [[esc(k), f'<span class="num">{v:.2f}</span>', bar(v, 10)]
                  for k, v in top]

    # Two different reasons for an empty table, and they are not interchangeable.
    # The live codex is the second one: v3 holds "no synths" and "no pads" and
    # nothing else, both well over the observation floor. Saying nothing had
    # enough observations there would be false about the only two rows there are.
    if styles and not scoreable:
        styles_empty = (f"every one of the {dropped} stored descriptor"
                        f"{'' if dropped == 1 else 's'} is a negation, so none of "
                        f"them can be scored — see the note below")
    else:
        styles_empty = "no descriptor has enough observations behind it yet"

    # Read through codex.scored, which is what brief_context() reads, and
    # parsed before it is sorted. Sorting first meant indexing every stored
    # value as a dict, so a body holding a bare number — a shape scored() is
    # written to keep readable forever — took this page down while the prompt
    # path rendered it happily. The page must not be stricter than the prompt.
    genre_scored = [(label, pair) for label, pair
                    in ((k, codex_scored(v))
                        for k, v in (learned.get("genre_scores") or {}).items())
                    if pair is not None]
    genre_rows = [[
        f"<b>{esc(label)}</b>",
        f'<span class="num">{mean:.2f}</span>',
        f'<span class="mini">over {n} rated brief{"" if n == 1 else "s"}</span>',
        bar(mean, 10),
    ] for label, (mean, n) in sorted(genre_scored, key=lambda kv: (-kv[1][0], kv[0]))]

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
        (f'<p class="sub">Nothing scoreable yet — {stats_now["observations"]} '
         f'observations recorded, and an average needs at least four behind it '
         f'before it is reported as a trend.</p>' if not top else
         '<p class="sub">Style descriptors scored by outcome. Your ratings win '
         'outright where they exist; the Producer\'s score stands in, damped, where '
         'they do not.</p>'),
        table(["Style descriptor", "Score", ""], learn_rows,
              empty=styles_empty, num_cols={1}),
        (f'<p class="sub">{dropped} stored descriptor'
         f'{"" if dropped == 1 else "s"} not shown: they state what a record '
         f'does not have, and an absence cannot be credited with an outcome in '
         f'either direction. They are still in the stored body — a codex '
         f'version is never rewritten in place — and they reach neither this '
         f'table nor the Director.</p>' if dropped else ""),
        "<h2>Genre, scored by your ratings</h2>",
        f'<p class="sub">Counted over distinct briefs, never clips, and shown '
        f'only once {genres.GENRE_MIN_RATED} rated briefs sit behind the label. '
        f'The full record, the day\'s slate and what it is waiting for are on '
        f'<a href="/genres">Genres</a>.</p>',
        table(["Genre", "Your mean", "Sample", ""], genre_rows,
              empty=f"no genre has {genres.GENRE_MIN_RATED} rated briefs behind "
                    f"it yet", num_cols={1}),
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


# ── genres ───────────────────────────────────────────────────────────────────
# Not a section on /codex. That page is a document viewer — the Director's
# working paper, versioned and never edited in place. "Which genre is working"
# is a daily question needing four tables and a time axis, and half of what it
# shows (today's slate, the explore ledger, the chart split) is run data rather
# than codex data.
CONFOUND = (
    "Genre is entangled with persona and this is not a controlled experiment. "
    "Vale sings restrained alt-pop, Rook electronic soul, Marisol warm uptempo "
    "live-band, and a spec is assigned its persona after it has its genre — so "
    "\"country scores 7.4\" may be \"Marisol scores 7.4\", and nothing here can "
    "tell those apart. There is one rater. They see the title, the artwork and "
    "the persona before scoring, and they know which songs the studio was "
    "betting on. Nothing is blinded, nothing is randomised, and the slate goes "
    "to the least-sampled families rather than to a control group. Read these "
    "numbers as a record of what you liked, not as a finding about genre."
)

STANCE_PILL = {"exploit": "ok", "hold": "dim", "explore": "cool"}


def _genre_mean_cell(row: dict) -> str:
    """The mean, or a dash and the distance left to the bar. Never a bare number.

    Today's /codex renders "no synths 6.11" under a heading saying it was
    observed to score well, with no sample count anywhere on the row. That is
    how an average over two decisions comes to look like a finding, and a genre
    label repeats by construction, so the same number here would look solid
    sooner. The count is part of the value, not a footnote to it.
    """
    if row["taste"] is not None:
        return (f'<b>{row["taste"]:.1f}</b><br><span class="mini">over '
                f'{row["rated_n"]} rated briefs</span>')
    return (f'—<br><span class="mini">{row["rated_n"]} of {genres.GENRE_MIN_RATED} '
            f'rated briefs</span>')


def _genre_producer_cell(row: dict) -> str:
    """The Producer's own mean, with the clips it was taken over.

    Same treatment as the rating mean beside it, and it needs it more, not
    less. This one is available from day one, so the first number a reader ever
    sees on this page is here — and it is an average over CLIPS, so one brief
    fills it with two scores that are not two decisions. A bare "9.4" in the
    row that sorts to the top of an otherwise empty table is exactly how a
    two-clip average comes to look like a finding.
    """
    if row["producer"] is None:
        return '<span class="mini">—</span>'
    n = row.get("producer_n") or 0
    return (f'<span class="num">{row["producer"]:.1f}</span><br>'
            f'<span class="mini">over {n} scored clip{"" if n == 1 else "s"}</span>')


def _genre_delta_cell(row: dict) -> str:
    if row.get("delta") is None:
        return '<span class="mini">not enough history</span>'
    return f'{row["delta"]:+.1f}'


def _genre_row(label: str, row: dict, trend_row: dict) -> list[str]:
    rel = row["reliability"]
    return [
        f"<b>{esc(label)}</b>",
        f'<span class="num">{row["briefed"]}</span>',
        f'<span class="num">{row["shipped"]}</span>',
        f'<span class="num">{row["rated_n"]}</span>',
        _genre_mean_cell(row),
        _genre_producer_cell(row),
        (f'<span class="num">{rel:.0%}</span><br>'
         f'<span class="mini">of {row["qc_measured"]} clips</span>'
         if rel is not None else '<span class="mini">—</span>'),
        _genre_delta_cell(trend_row),
        f'<span class="mini">{esc(row["last_briefed"] or "never")}</span>',
    ]


def _genre_headline(data: dict, learn: dict) -> str:
    fams = data["families"]
    tried = len(data["briefed_families"])
    ranked = data["ranked"]
    labelled = sum(r["briefed"] for r in fams.values())
    n_fam, n_spec = len(genres.FAMILIES), len(genres.SPECIFICS)

    if not tried:
        return (f"No genre has been briefed yet. The studio can name {n_fam} "
                f"families and {n_spec} specific genres and has recorded none of "
                f"them. The first run fills this page.")
    if not ranked:
        return (f"{tried} of {n_fam} families briefed across {labelled} "
                f"brief{'' if labelled == 1 else 's'}, and nothing here is "
                f"ranked: {learn['rated']} of {learn['shipped']} shipped "
                f"song{'' if learn['shipped'] == 1 else 's'} carry your rating, "
                f"so the only column with numbers in it is the Producer's own "
                f"score. {learn['signal']}.")

    order = sorted(ranked, key=lambda f: (-fams[f]["taste"], f))
    lead, top = _genre_leaders(data)
    if len(order) == 1:
        head = (f"{order[0]} is the only ranked family: {top:.1f} across "
                f"{fams[order[0]]['rated_n']} rated briefs, with nothing yet "
                f"to compare it against. ")
    elif len(lead) == len(order):
        # Every ranked family on the same number. "X is ahead, Y trails" beside
        # two identical figures was the version of this sentence a reader could
        # check against the table and find false.
        head = (f"{_and(lead)} are level at {top:.1f}, across "
                f"{_and([str(fams[f]['rated_n']) for f in lead])} rated briefs "
                f"respectively — nothing separates them. ")
    else:
        lag = order[-1]
        ahead = (f"{lead[0]} is ahead: {top:.1f} across "
                 f"{fams[lead[0]]['rated_n']} rated briefs. " if len(lead) == 1
                 else f"{_and(lead)} are level at the top: {top:.1f}. ")
        head = (ahead + f"{lag} trails at {fams[lag]['taste']:.1f} across "
                        f"{fams[lag]['rated_n']}. ")
    unranked = tried - len(ranked)
    if not unranked:
        return head + (f"Every briefed family is ranked, which takes "
                       f"{genres.GENRE_MIN_RATED} rated briefs each — the "
                       f"numbers move from here, not the count.")
    return head + (f"{unranked} of {tried} briefed families are not ranked — a "
                   f"family needs {genres.GENRE_MIN_RATED} rated briefs behind "
                   f"it before an average means anything.")


def _genre_waiting(data: dict, cfg) -> str:
    """The rule and the current count. Never an ETA.

    An ETA needs an assumption about tomorrow's slate and tomorrow's ratings,
    and assuming a track record is the thing this page exists not to do.
    """
    fams = data["families"]
    bar_n = genres.GENRE_MIN_RATED
    rule = f"A family is ranked once {bar_n} rated briefs sit behind it. "
    close = [f for f in fams if fams[f]["taste"] is None and fams[f]["rated_n"]]
    if close:
        best = max(close, key=lambda f: (fams[f]["rated_n"], f))
        return rule + f"The closest is {best} with {fams[best]['rated_n']}."

    # No family is part-way to the bar, and that has two opposite causes. This
    # is the one where every family carrying a rating has already cleared it —
    # the design's intended end state — and it used to fall through to the
    # branch below and tell the user nothing had been rated, directly under a
    # headline naming a leader. Checked first for that reason.
    ranked = data["ranked"]
    if ranked:
        unrated = [f for f in fams if not fams[f]["rated_n"]]
        head = (rule + f"Every family carrying a rating has cleared it: "
                       f"{len(ranked)} of {len(fams)} ranked, and nothing is "
                       f"part-way. ")
        if not unrated:
            return head + ("There is no family left to promote — the numbers "
                           "move from here, not the count.")
        briefed = [f for f in unrated if fams[f]["briefed"]]
        # Only the clauses that are not zero. "0 briefed and waiting on you"
        # reads as a finding about nothing.
        parts = [f"{n} {what}" for n, what in
                 ((len(briefed), "briefed and waiting on you"),
                  (len(unrated) - len(briefed), "waiting on a slate")) if n]
        return head + (f"The other {len(unrated)} have no rating behind them at "
                       f"all: {_and(parts)}.")

    labelled = sum(r["briefed"] for r in fams.values())
    if labelled:
        return rule + (f"{labelled} brief{'' if labelled == 1 else 's'} carry a "
                       f"genre and none of them has been rated, so nothing is "
                       f"close. Rating the songs on the overview is the only "
                       f"thing that moves this page.")
    return rule + (f"Nothing has been briefed, so nothing is close. The next "
                   f"run allocates {cfg.total_briefs} briefs across the "
                   f"least-sampled families, which today is all "
                   f"{len(genres.FAMILIES)} of them.")


def _genre_slate_block(latest: tuple[date, dict] | None, cfg,
                       sense: tuple[date, dict] | None = None) -> tuple[str, str]:
    """Today's slate if a run recorded one, otherwise the one the next run gets.

    Computing it rather than showing an empty box is what makes the page real
    on day one: the allocator is deterministic, so an all-explore slate is an
    honest thing to look at. It is labelled as unrun, because a plan is not a
    record.

    Given the same two arguments the pipeline gives it, and this is not
    optional. ``_coverage_pick`` sorts a family the charts named ahead of one
    they did not, among families sampled equally — which on an empty database
    is the entire ordering. Calling it bare here showed a preview with folk in
    it and r-and-b out where the run would have done the reverse, under a
    caption promising it was the same slate. The caption below now says what it
    actually rests on as well, because the next run senses the feeds again
    before it allocates and the charts move.
    """
    if latest is not None:
        run_date, ledger_notes = latest
        rows = ledger_notes.get("slate") or []
        today = run_date == date.today()
        heading = "Today's slate" if today else f"The slate of {run_date.isoformat()}"
        caption = (f"Recorded on {run_date.isoformat()}, in the same write as "
                   f"the briefs it allocated — a slate persisted without them "
                   f"would be a plan nothing carried out.")
    else:
        sense_date, sheet = sense if sense else (None, {})
        rows = genres.slate(cfg.total_briefs,
                            calibration=sheet.get("calibration"),
                            external=sheet.get("external_genres"))
        heading = "The next slate"
        caption = ("No run has recorded a slate yet. This is what the allocator "
                   "returns for " + (
                       f"the chart evidence of {sense_date.isoformat()}"
                       if sense_date else "no chart evidence at all")
                   + " — it is deterministic, so the next run gets this slate "
                     "unless something is rated first or the charts move under "
                     "it, and the next run senses the charts before it allocates.")

    body = [[
        f"<b>{esc(r.get('genre_family'))}</b>",
        f'<span class="num">{r.get("specs")}</span>',
        pill(str(r.get("stance") or "explore"),
             STANCE_PILL.get(r.get("stance"), "cool")),
        (f'{r["mean"]:.1f} <span class="mini">n={r.get("n", 0)}</span>'
         if r.get("mean") is not None else
         f'— <span class="mini">n={r.get("n", 0)}</span>'),
        (f'+{r["bonus"]:.3f}' if r.get("bonus") is not None
         else '<span class="mini">—</span>'),
        f'<span class="mini">{esc(r.get("why") or r.get("basis") or "")}</span>',
    ] for r in rows]

    explore = sum(int(r.get("specs") or 0) for r in rows if r.get("stance") == "explore")
    rhythm = sum(int(r.get("specs") or 0) for r in rows if r.get("stance") == "floor")
    total = sum(int(r.get("specs") or 0) for r in rows)
    if total and explore == total:
        floor_line = (f"Every one of these {total} briefs is exploration: "
                      f"{explore} of {total} go to families with no evidence "
                      f"behind them. That is not a strategy, it is the absence "
                      f"of one, and it is the honest place to start.")
    elif total:
        floor_line = (f"{explore} of {total} briefs are exploration, against a "
                      f"floor of {cfg.genre_explore_briefs}. The floor is what "
                      f"keeps the chart, the Scout, the Director and the codex "
                      f"from being the only things that talk to each other.")
    else:
        floor_line = ""

    # Reported separately and never folded into the exploration count, because
    # they are not the same kind of decision. An exploration pick is the
    # allocator admitting it does not know; a rhythm pick is the operator
    # deciding something regardless of what it knows. Putting them in one number
    # would make the page claim more evidence behind the slate than there is.
    if rhythm:
        floor_line += (f" A further {rhythm} of {total} "
                       f"{'is' if rhythm == 1 else 'are'} the rhythm floor: "
                       f"reserved for {', '.join(sorted(genres.RHYTHM_LED))} "
                       f"whatever the ratings say, because this catalogue is made "
                       f"to be used in vertical video and a day of ballads cannot "
                       f"be. That is a taste decision, not a measurement, and it "
                       f"biases the record — those families accumulate rated "
                       f"briefs faster than the rest.")

    return heading, "".join([
        f'<p class="sub">{esc(caption)}</p>',
        table(["Family", "Songs", "Stance", "Observed", "Uncertainty", "Why picked"],
              body, empty="no slate — the allocator returned nothing",
              num_cols={1}),
        (f'<p class="sub">{esc(floor_line)}</p>' if floor_line else ""),
    ])


def _genre_history(s, days: int = 30) -> str:
    since = date.today() - timedelta(days=days)
    mix = s.execute(
        select(Run.run_date, Brief.genre_family, func.count(Brief.id))
        .join(Brief, Brief.run_id == Run.id)
        .where(Run.run_date >= since)
        .group_by(Run.run_date, Brief.genre_family)).all()
    notes = dict(s.execute(
        select(Run.run_date, Run.notes).where(Run.run_date >= since)).all())

    by_day: dict[date, dict[str | None, int]] = {}
    for run_date, family, count in mix:
        by_day.setdefault(run_date, {})[family] = count

    rows = []
    for run_date in sorted(by_day, reverse=True):
        counts = by_day[run_date]
        total = sum(counts.values())
        cells = "".join(
            f'<div><span class="mini">{esc(fam or "no genre")} {n}</span>'
            f"{bar(n, total)}</div>"
            for fam, n in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0]))))
        slate = ((notes.get(run_date) or {}).get("genre") or {}).get("slate") or []
        explore = sum(int(r.get("specs") or 0) for r in slate
                      if r.get("stance") == "explore")
        rows.append([
            f'<a class="q" href="/runs/{run_date.isoformat()}">{run_date.isoformat()}</a>',
            f'<span class="num">{total}</span>',
            cells,
            (f'<span class="num">{explore}</span>' if slate
             else '<span class="mini">—</span>'),
            f'<span class="num">{counts.get(None, 0)}</span>',
        ])
    return table(["Date", "Briefs", "Mix", "Explore", "No genre"], rows,
                 empty=f"no briefs in the last {days} days",
                 num_cols={1, 3, 4})


def _genre_external(latest_scout: tuple[date, dict] | None) -> str:
    """What the outside feeds said, with the two Apple scopes kept apart."""
    if latest_scout is None:
        return ('<div class="note"><span>Outside evidence</span>No run has '
                'fetched a chart yet. External feeds pick which families are '
                'live candidates; they never produce a score, and the ratings '
                'above are the only thing that ranks anything.</div>')
    run_date, ext = latest_scout
    current = ext.get("current") or {}
    catalogue = ext.get("catalogue") or {}
    deezer = ext.get("deezer") or {}
    sources = ", ".join(ext.get("sources") or []) or "none"

    def lead(counts: dict) -> str:
        """The leader, or the tie spelled out.

        A tie broken silently is the whole failure this split exists to fix:
        the blended US chart reads country 23 to pop 6, and on current releases
        alone the same feed is 6-6. Printing one name there would hide the
        finding behind a sort order.
        """
        if not counts:
            return "nothing"
        top = max(counts.values())
        names = sorted(k for k, v in counts.items() if v == top)
        if len(names) == 1:
            return f"{names[0]} on {top}"
        return (", ".join(names[:-1]) + f" and {names[-1]} tied on {top}")

    blended: dict[str, int] = {}
    for src in (current, catalogue):
        for k, v in src.items():
            blended[k] = blended.get(k, 0) + v

    entries = ext.get("entries") or {}
    lines = [
        f"Genre evidence on {run_date.isoformat()}, from {sources}. Apple is a "
        f"most-played chart and lags by construction, so the current-release "
        f"count and the catalogue count are kept apart rather than added: "
        f"blended it is {lead(blended)}; on current releases alone it is "
        f"{lead(current)}. That difference is the reason for the split.",
        f"Apple: {entries.get('apple_current', 0)} current entries, "
        f"{entries.get('apple_catalogue', 0)} catalogue. Deezer, a different "
        f"population answering the same question, leads with {lead(deezer)} over "
        f"{entries.get('deezer', 0)} albums. Where the two disagree that is a "
        f"reason to hold confidence down, not to pick a side.",
    ]
    unrecognised = ext.get("unrecognised") or {}
    missing = sorted({k for v in unrecognised.values() for k in v})
    if missing:
        lines.append("Ids the vocabulary has never seen, which is a reason to "
                     "edit genres.py rather than to guess: " + ", ".join(missing[:6]))
    outside = ext.get("outside_roster") or {}
    excluded = sorted({k for v in outside.values() for k in v})
    if excluded:
        lines.append("On the charts and deliberately outside the roster: "
                     + ", ".join(excluded[:8]) + ". Counted, never forced into a "
                     "family they do not belong to.")
    return ('<div class="note"><span>Outside evidence</span>'
            + "<br><br>".join(esc(x) for x in lines) + "</div>")


def genres_page() -> str:
    cfg = settings()
    data = genres.scores()
    st = genres.status(data=data)
    learn = learning_status()
    moves = genres.trend()
    fams = data["families"]

    with session_scope() as s:
        recent = s.execute(select(Run.run_date, Run.notes)
                           .order_by(Run.run_date.desc()).limit(60)).all()
        history = _genre_history(s)
    latest_slate = next(((d, (n or {})["genre"]) for d, n in recent
                         if (n or {}).get("genre", {}).get("slate")), None)
    # The whole scout block, not just its chart counts: the preview slate needs
    # the same calibration and external pair the pipeline hands the allocator,
    # and they were written by one run together.
    latest_sense = next(((d, (n or {})["scout"]) for d, n in recent
                         if (n or {}).get("scout", {}).get("external_genres")), None)
    latest_scout = ((latest_sense[0], latest_sense[1]["external_genres"])
                    if latest_sense else None)

    # Ranked families first and in score order, then the rest by how much has
    # been tried. The producer's own score is reported and never orders
    # anything: it is the system grading its own homework, and letting it
    # decide what appears at the top of this page would make it the answer to
    # "which genre is working" by layout rather than by argument.
    order = sorted(fams, key=lambda f: (fams[f]["taste"] is None,
                                        -(fams[f]["taste"] or 0.0),
                                        -fams[f]["briefed"], f))
    family_rows = [_genre_row(f, fams[f], moves.get(f, {})) for f in order]

    # Grouped under the family and in the family order above, so the two
    # tables cannot be read as disagreeing about which family is ahead.
    rank = {f: i for i, f in enumerate(order)}
    spec_rows = [_genre_row(label, row, {})
                 for label, row in sorted(
                     data["specifics"].items(),
                     key=lambda kv: (rank.get(kv[1]["family"], 99), kv[0]))
                 if row["briefed"] or row["rated_n"]]

    vocab_rows = [[
        f"<b>{esc(family)}</b>",
        f'<span class="mini">{esc(genres.id3_name(family))}</span>',
        f'<span class="mini">{esc(", ".join(genres.VOCABULARY[family]))}</span>',
    ] for family in genres.FAMILIES]

    slate_heading, slate_body = _genre_slate_block(latest_slate, cfg,
                                                   sense=latest_sense)

    unlabelled = data["unlabelled_briefs"]
    predate = ("" if not unlabelled else
               f'<div class="note"><span>Before genre was recorded</span>'
               f'{unlabelled} brief{"" if unlabelled == 1 else "s"} carry no '
               f'genre at all. They were written before the studio recorded one '
               f'and they keep a null rather than a guess: reading a genre back '
               f'out of their style strings afterwards would be inventing the '
               f'track record this page exists to report. They are not an error '
               f'and there is nothing to fix.</div>')

    ledger_notes = latest_slate[1] if latest_slate else {}
    off_vocab = int(ledger_notes.get("unlabelled") or 0)
    # Dated, like the slate heading below it. This is the most recent run that
    # recorded a slate, which is not necessarily the most recent run: a run that
    # fails in sense or brief never writes this key, so "on the last run" could
    # silently mean three days ago on exactly the days an operator is reading
    # the page to find out what went wrong.
    off_when = (f"on the run of {latest_slate[0].isoformat()}" if latest_slate
                else "on the last recorded run")
    words = [w for w in (ledger_notes.get("off_vocabulary") or []) if w]
    off_words = ("" if not words else
                 " The words it chose were " + esc(_and(sorted(set(words)))) +
                 " — that is the term to argue about, not the count.")
    off_note = ("" if not off_vocab else
                f'<div class="note"><span>Vocabulary pressure</span>The Director '
                f'returned a genre outside the vocabulary {off_vocab} time'
                f'{"" if off_vocab == 1 else "s"} {off_when}.{off_words} Those '
                f'specs were briefed and written with a null genre rather than '
                f'dropped — a discarded spec costs a whole generation over a '
                f'label. A count that keeps climbing is the evidence that a term '
                f'is missing, and adding one is a change to genres.py.</div>')

    proposal = (current_codex().body.get("learned", {})
                .get("genre_vocabulary_note") or "").strip()
    proposal_note = ("" if not proposal else
                     f'<div class="note"><span>The retro proposes</span>'
                     f'{esc(proposal)}<br><br>A proposal only. The vocabulary is '
                     f'code, not codex, so adding a label is an edit to '
                     f'genres.py and a deploy — which is what makes it land in '
                     f'the briefer, the archivist, the duplicate check, the ID3 '
                     f'tagger and the meta writer at the same moment.</div>')

    lastfm = ("" if cfg.lastfm_api_key else
              '<p class="sub">Last.fm is not configured, and configuring it '
              'would not help: chart.getTopTags takes no date and no period, so '
              'it returns a level rather than a movement. That is a decision, '
              'not a gap.</p>')

    return page("Genres", "".join([
        "<h1>Genres</h1>",
        '<p class="sub">Which genre is working, and what it would take to know. '
        'Outside charts pick which families are candidates; your ratings are the '
        'only thing that ranks them.</p>',
        stats(("in the vocabulary", len(genres.FAMILIES)),
              ("briefed", len(data["briefed_families"])),
              ("ranked", len(data["ranked"])),
              ("rated briefs", data["rated_briefs"]),
              ("explore floor",
               f"{cfg.genre_explore_briefs} of {cfg.total_briefs}")),
        f'<p class="sub">{esc(_genre_headline(data, learn))}</p>',
        f'<div class="note"><span>Allocator</span>{esc(st["note"])}<br><br>'
        f'{esc(_genre_waiting(data, cfg))}</div>',
        f'<div class="note"><span>Read this before the numbers</span>'
        f'{esc(CONFOUND)}</div>',
        predate, off_note, proposal_note,
        "<h2>What is working</h2>",
        table(["Family", "Briefed", "Shipped", "Rated", "Your mean",
               "Producer mean", "Renders clean", "14d vs prior", "Last briefed"],
              family_rows, empty="the vocabulary is empty, which cannot happen",
              num_cols={1, 2, 3, 5, 6, 7}),
        '<p class="sub">Briefed counts briefs, not clips: each brief returns two '
        'clips that share a prompt and are not two independent samples. Your '
        'mean is your rating and nothing else — the Producer\'s own score is '
        'shown beside it, never blended into it, and never used to order this '
        'table. Renders clean is the QC pass rate, which is the one column here '
        'that is objective and the one available on day one.</p>',
        f"<h2>{esc(slate_heading)}</h2>", slate_body,
        "<h2>The last 30 days</h2>",
        history,
        "<h2>Specific genres</h2>",
        table(["Genre", "Briefed", "Shipped", "Rated", "Your mean",
               "Producer mean", "Renders clean", "14d vs prior", "Last briefed"],
              spec_rows, num_cols={1, 2, 3, 5, 6, 7},
              empty=f"no specific genre has been briefed yet — all "
                    f"{len(genres.SPECIFICS)} of them are listed below"),
        f'<p class="sub">Mostly dashes, and that is the design working rather '
        f'than failing: a specific needs the same {genres.GENRE_MIN_RATED} '
        f'rated briefs a family does, drawn from one of '
        f'{len(genres.SPECIFICS)} labels rather than one of '
        f'{len(genres.FAMILIES)}. So the family table answers "is country '
        f'working" first and this one answers "is it country-soul or '
        f'country-trap" a long way behind it. How long is not stated here on '
        f'purpose — it depends entirely on how many of the day\'s songs get '
        f'rated, which today is {learn["rated"]} of {learn["shipped"]}.</p>',
        "<h2>The vocabulary</h2>",
        table(["Family", "ID3 tag", "Specific genres"], vocab_rows),
        '<p class="sub">Closed, and code rather than codex: the Director, the '
        'Archivist, the duplicate check, the ID3 tagger and the meta writer all '
        'have to agree on it, and a deploy changes all five at once. A label '
        'that has shipped is never renamed and never removed — renaming orphans '
        'every row carrying it and splits one track record into two halves, '
        'neither of which clears the bar.</p>',
        _genre_external(latest_scout),
        lastfm,
    ]), "/genres")


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
    # cover.jpg is the one basename the ship loop may not write, so the tree is
    # drawn from ALWAYS_SHIPPED plus the same key the loop branches on rather
    # than typed out. A fixed listing goes stale in whichever direction it was
    # written: this one said five files and no artwork whatever the deployment.
    per_song = list(ALWAYS_SHIPPED) + (["cover.jpg"] if cfg.ark_api_key else [])
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
        f'├─ 01_slug/  {esc(" · ".join(per_song))}\n'
        f'└─ _rejected/rejects.json</pre>',
        *(blocks or ['<div class="empty">nothing delivered yet</div>']),
    ]), "/files")
