"""Console page bodies. Each reads the database and returns HTML."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select

from .. import ledger, llm
from ..archivist import aggregate, learning_status
from ..codex import current as current_codex
from ..config import settings
from ..db import session_scope
from ..models import (AgentCall, Brief, Clip, CodexVersion, Decision, Job,
                      Outcome, Run, RunPhase, Signal)
from .console import (ago, bar, dur, esc, jsonblock, ms, page, pill, stats,
                      table)

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

    body = [
        "<h1>Overview</h1>",
        f'<p class="sub">Five finished songs a day, unattended. '
        f'{esc(cfg.full_slots)} full-length and {esc(cfg.short_slots)} short-form, '
        f'chosen from {esc(cfg.total_briefs * 2)} candidates.</p>',
        stats(("runs", st["runs"]), ("clips", st["clips"]), ("shipped", st["shipped"]),
              ("rated", st["rated"]), ("credits", spent or "—")),
        f'<div class="note"><span>Learning signal</span>{esc(st["signal"])}</div>',
        "<h2>Brains</h2>", _brain_table(),
        "<h2>Recent runs</h2>",
        table(["Date", "Phase", "Clips", "Shipped", "Cut", "Rated", "Credits", ""],
              rows, empty="no runs yet — `dailyfive run` starts one",
              num_cols={2, 3, 4, 5, 6}),
    ]
    return page("Overview", "".join(body), "/")


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
    return [
        f'<a class="q" href="/runs/{r.run_date.isoformat()}">{esc(r.run_date)}</a>',
        pill(r.phase.value, kind),
        f'<span class="num">{len(clips)}</span>',
        f'<span class="num">{shipped}</span>',
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

        files = [[
            f'<b>{esc(c.title)}</b>',
            f'<span class="mini">{esc(c.spaces_key or "not uploaded")}</span>',
            pill("wav ready", "ok") if c.wav_url else pill("mp3 only", "dim"),
        ] for c in clips if c.shipped]

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
        table(["Song", "Spaces key", "Master"], files, empty="nothing delivered yet"),
    ]
    return page(str(run_date), "".join(body), "/runs")


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

    by_day: dict[str, list] = {}
    for clip, run_date, persona in rows_db:
        by_day.setdefault(run_date.isoformat(), []).append((clip, persona))

    blocks = []
    for day, entries in by_day.items():
        rows = [[
            f'<b>{esc(c.title)}</b>',
            pill(c.slot_type.value, "cool" if c.slot_type.value == "short" else "dim"),
            esc(persona or "—"),
            f'<span class="num">{dur(c.duration_s)}</span>',
            f'<span class="mini">{esc(c.spaces_key or "—")}</span>',
            '<span class="mini">wav · mp3 · cover · lyrics · lrc · meta</span>',
        ] for c, persona in entries]
        blocks += [f"<h2>{esc(day)}</h2>",
                   table(["Song", "Slot", "Persona", "Length", "Key prefix",
                          "Files"], rows)]

    prefix = f"{cfg.spaces_bucket or '<bucket>'}/{cfg.spaces_prefix}"
    return page("Files", "".join([
        "<h1>Delivered files</h1>",
        '<p class="sub">Everything this studio has shipped. One immutable dated '
        'folder per run, mirrored the moment the bytes existed — Suno deletes its '
        'own copies after 15 days.</p>',
        f'<pre>spaces://{esc(prefix)}/YYYY-MM-DD/\n'
        f'├─ manifest.json\n├─ index.html          '
        f'<i>the rating page</i>\n'
        f'├─ 01_slug/  master.wav · master.mp3 · cover.jpg · lyrics.txt · '
        f'lyrics.lrc · meta.json\n└─ _rejected/rejects.json</pre>',
        *(blocks or ['<div class="empty">nothing delivered yet</div>']),
    ]), "/files")
