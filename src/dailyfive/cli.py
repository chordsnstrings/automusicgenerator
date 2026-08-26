"""Command line. Everything you need to operate the studio without editing code."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime

from sqlalchemy import select

from . import archivist, codex
from .config import settings
from .db import init_db, session_scope
from .errors import DailyFiveError, ProviderError
from .models import Brief, Clip, Job, Run, SlotType

log = logging.getLogger("dailyfive")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_preflight(args) -> int:
    from .pipeline import preflight
    init_db()
    problems = preflight(require_ffmpeg=not args.no_ffmpeg)
    if not problems:
        print("preflight: all checks passed — ready to run")
        return 0
    print(f"preflight: {len(problems)} problem(s)\n")
    for p in problems:
        print(f"  ✗ {p}")
    print("\nFix these before the first run. Nothing above costs credits to re-check.")
    return 1


def cmd_run(args) -> int:
    from .pipeline import preflight, run_daily
    init_db()
    if not args.force:
        problems = preflight(require_ffmpeg=True)
        if problems:
            print("refusing to start — preflight failed:")
            for p in problems:
                print(f"  ✗ {p}")
            print("\nRe-run with --force to start anyway (not recommended).")
            return 1
    run_date = _parse_date(args.date)
    summary = run_daily(run_date, resume=not args.no_resume, skip_art=args.skip_art)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_signals(args) -> int:
    """Test the trend feeds without spending anything."""
    from .signals import collect_all
    feeds = collect_all(args.region)
    live = [f for f in feeds if f.ok]
    for f in feeds:
        mark = "✓" if f.ok else "✗"
        print(f"  {mark} {f.summary()}")
    print(f"\n{len(live)}/{len(feeds)} feeds live.")
    if args.json:
        print(json.dumps([{"source": f.source, "lead": f.lead,
                           "items": f.items[:5], "error": f.error} for f in feeds],
                         indent=2, default=str))
    if not live:
        print("\nEvery feed failed. The Scout refuses to invent themes from nothing.")
        return 1
    if len(live) < 3:
        print("\nOnly a thin signal is available. Themes will lean on fewer sources;\n"
              "the optional API keys in .env.example widen this considerably.")
    return 0


def cmd_personas(args) -> int:
    init_db()
    if args.action == "list":
        cx = codex.current()
        print(f"codex v{cx.version} — {len(cx.personas)} personas, "
              f"{len(cx.personas_ready())} registered\n")
        for p in cx.personas:
            state = p.get("persona_id") or "NOT REGISTERED"
            print(f"  {p.get('name'):<12} {p.get('vocal_gender') or '?':<3} {state}")
            print(f"  {'':<12}     {p.get('territory', '')[:70]}")
        if not cx.personas_ready():
            print("\nRun `dailyfive personas bootstrap` to create them on Suno's side.")
        return 0

    if args.action == "set":
        if not (args.name and args.persona_id):
            print("usage: dailyfive personas set --name Vale --persona-id psn_...")
            return 2
        v = codex.set_persona_id(args.name, args.persona_id, args.persona_model)
        print(f"registered {args.name} -> {args.persona_id} (codex v{v})")
        return 0

    return _bootstrap_personas(args)


def _bootstrap_personas(args) -> int:
    """Create a Suno persona per seed act.

    Each one needs a finished generation to sample from, so this generates a
    seed song per persona and then builds the persona from a vocal segment of
    it. Costs credits — one generation per unregistered persona.
    """
    from .agents.compiler import compile_payload, strip_internal
    from .conductor import Conductor
    from .models import JobState, Run, RunPhase
    from .providers.suno import SunoClient

    cx = codex.current()
    todo = [p for p in cx.personas if not p.get("persona_id")]
    if not todo:
        print("every persona is already registered — nothing to do")
        return 0

    print(f"{len(todo)} persona(s) to create. This costs one generation each.")
    if not args.yes:
        reply = input("proceed? [y/N] ").strip().lower()
        if reply != "y":
            print("aborted")
            return 1

    client = SunoClient()
    with session_scope() as s:
        run = Run(run_date=date.today(), phase=RunPhase.CREATED,
                  notes={"kind": "persona-bootstrap"})
        existing = s.execute(select(Run).where(Run.run_date == date.today())).scalar_one_or_none()
        if existing:
            run = existing
        else:
            s.add(run)
            s.flush()
        run_id = run.id

    created = 0
    for persona in todo:
        name = persona.get("name")
        print(f"\n— {name} —")
        brief = _seed_brief(persona)
        with session_scope() as s:
            b = Brief(run_id=run_id, slot_type=SlotType.FULL,
                      idx=900 + created, title=brief["title"], theme=brief["theme"],
                      persona_name=name, bpm=brief["bpm"], musical_key=brief["key"],
                      song_form=brief["song_form"], vocal_gender=brief["vocal_gender"],
                      style_string=brief["style_string"], lyrics=brief["lyrics"],
                      negative_tags=cx.negative_tags())
            s.add(b)
            s.flush()
            brief_id = b.id

        payload = compile_payload(brief)
        with session_scope() as s:
            s.add(Job(run_id=run_id, brief_id=brief_id,
                      idempotency_key=f"persona:{name}:{run_id}",
                      payload=strip_internal(payload), state=JobState.QUEUED))

        cond = Conductor(run_id, client=client)
        try:
            cond.submit_all()
            print("  generating (this takes a couple of minutes)…")
            cond.await_all(timeout_s=900)
        except DailyFiveError as exc:
            print(f"  ✗ generation failed: {exc}")
            continue

        with session_scope() as s:
            clip = s.execute(
                select(Clip).where(Clip.brief_id == brief_id)
                .order_by(Clip.variant)).scalars().first()
            if clip is None:
                print("  ✗ no clip came back")
                continue
            task_id = clip.job.task_id
            audio_id = clip.audio_id
            duration = clip.duration_s or 60.0

        # Sample from 20s in, where a vocal is likeliest, within the 10-30s rule.
        start = min(20.0, max(0.0, duration - 32.0))
        end = min(start + 25.0, duration - 1.0)
        if end - start < 10.0:
            start, end = 0.0, min(30.0, max(10.5, duration - 0.5))

        try:
            persona_id = client.create_persona(
                task_id, audio_id, name=name,
                description=persona.get("territory", "")[:900],
                style=(brief["style_string"] or "")[:200],
                vocal_start=start, vocal_end=end)
        except ProviderError as exc:
            print(f"  ✗ persona creation failed: {exc}")
            continue

        codex.set_persona_id(name, persona_id, "style_persona")
        print(f"  ✓ {name} -> {persona_id}")
        created += 1

    print(f"\n{created}/{len(todo)} personas registered. "
          f"`dailyfive personas list` to confirm.")
    return 0 if created else 1


def _seed_brief(persona: dict) -> dict:
    """A deliberately plain song whose only job is to establish a voice."""
    gender = persona.get("vocal_gender") or "f"
    return {
        "title": f"{persona.get('name')} — Voice Seed",
        "slot_type": "full",
        "theme": persona.get("territory", ""),
        "bpm": 92,
        "key": "A minor",
        "song_form": "Intro(4) - Verse(16) - Chorus(16) - Verse(16) - Chorus(16) - Outro(4)",
        "vocal_gender": gender,
        "style_string": (persona.get("territory", "") +
                         ", clean vocal recording, clear diction, "
                         "uncluttered arrangement, vocal forward in the mix")[:900],
        "lyrics": (
            "[Verse]\nI counted every step across the floor\n"
            "The kettle clicked and settled into steam\n"
            "The light came slow and low along the wall\n"
            "And nothing here was harder than it seemed\n\n"
            "[Chorus]\nSo hold the line, hold the line for me\n"
            "I am learning how to sound like something true\n"
            "Hold the line, hold the line for me\n"
            "There is nothing in this room except the view\n\n"
            "[Verse]\nA neighbour's radio, a passing car\n"
            "The ordinary noise of getting through\n"
            "I put my coffee down and left it there\n"
            "And practised saying everything I knew\n\n"
            "[Chorus]\nSo hold the line, hold the line for me\n"
            "I am learning how to sound like something true\n"
            "Hold the line, hold the line for me\n"
            "There is nothing in this room except the view\n"
        ),
    }


def cmd_brains(args) -> int:
    """Which brain answers for which role, and whether it is reachable."""
    from . import llm, ledger
    cfg = settings()
    roster = llm.roster()
    summary = ledger.role_summary(30)
    needed = {"anthropic": "anthropic_api_key", "minimax": "minimax_api_key",
              "openai-compatible": "llm_api_key"}

    print(f"default: {cfg.llm_default or 'anthropic'}\n")
    print(f"  {'ROLE':<11} {'BRAIN':<34} {'KEY':<9} {'30d':>6}  NOTE")
    for role, brain in roster.items():
        attr = needed.get(brain.provider)
        if brain.provider == "unconfigured":
            key = "BAD"
        elif attr and not getattr(cfg, attr, ""):
            key = "missing"
        else:
            key = "ok"
        calls = summary.get(role, {}).get("calls", 0)
        print(f"  {role:<11} {str(brain):<34} {key:<9} {calls:>6}  "
              f"{llm.ROLE_HINTS.get(role, '')}")

    others = [(n, d) for n, r, d, _ in
              [("Prompt Compiler", None, "brief -> validated payload", 0),
               ("Conductor", None, "fires, polls, retries, mirrors", 0),
               ("QC Engineer", None, "ffmpeg measurement", 0),
               ("Packager", None, "master, art, tags, delivery", 0)]]
    print("\n  no brain, on purpose:")
    for name, does in others:
        print(f"    {name:<17} {does}")

    if not args.probe:
        print("\n  --probe sends a few tokens to each distinct brain to check it answers.")
        return 0

    print("\n  probing…")
    seen: dict[str, tuple[bool, str]] = {}
    failed = False
    for role, brain in roster.items():
        key = str(brain)
        if key not in seen:
            seen[key] = llm.probe(role)
        ok, detail = seen[key]
        failed = failed or not ok
        print(f"    {'OK ' if ok else 'FAIL'} {role:<11} {detail[:90]}")
    return 1 if failed else 0


def cmd_rate(args) -> int:
    init_db()
    with session_scope() as s:
        if s.get(Clip, args.clip_id) is None:
            print(f"no clip {args.clip_id}")
            return 1
    archivist.rate(args.clip_id, args.rating, args.note)
    st = archivist.learning_status()
    print(f"clip {args.clip_id} rated {args.rating}/10 — {st['signal']}")
    return 0


def cmd_unrate(args) -> int:
    """Take a rating back. A flat verb, because `rate --clear` would force the
    rating positional to nargs='?' and invent a `dailyfive rate 1` case argparse
    cannot refuse cleanly."""
    init_db()
    with session_scope() as s:
        if s.get(Clip, args.clip_id) is None:
            print(f"no clip {args.clip_id}")
            return 1
    cleared = archivist.unrate(args.clip_id)
    st = archivist.learning_status()
    print(f"clip {args.clip_id} " + ("rating cleared" if cleared else "was not rated")
          + f" — {st['signal']}")
    return 0


def cmd_status(args) -> int:
    init_db()
    st = archivist.learning_status()
    print(f"runs {st['runs']} · clips {st['clips']} · shipped {st['shipped']} · "
          f"rated {st['rated']}")
    print(f"learning signal: {st['signal']}\n")

    with session_scope() as s:
        runs = s.execute(select(Run).order_by(Run.run_date.desc())
                         .limit(args.limit)).scalars().all()
        for r in runs:
            clips = s.execute(select(Clip).where(Clip.run_id == r.id)).scalars().all()
            shipped = sum(1 for c in clips if c.shipped)
            failed = sum(1 for c in clips if c.qc_verdict == "fail")
            spent = r.credits_spent
            print(f"  {r.run_date}  {r.phase.value:<10} "
                  f"{len(clips):>2} clips · {shipped} shipped · {failed} cut"
                  + (f" · {spent} credits" if spent is not None else ""))
            if r.error:
                print(f"             error: {r.error[:110]}")
    return 0


def cmd_today(args) -> int:
    """The five shipped today, with their clip ids for rating."""
    init_db()
    run_date = _parse_date(args.date)
    with session_scope() as s:
        run = s.execute(select(Run).where(Run.run_date == run_date)).scalar_one_or_none()
        if run is None:
            print(f"no run for {run_date}")
            return 1
        clips = s.execute(
            select(Clip).where(Clip.run_id == run.id, Clip.shipped.is_(True))
            .order_by(Clip.slot_type, Clip.rank)).scalars().all()
        if not clips:
            print(f"run {run_date} is at phase {run.phase.value}; nothing shipped yet")
            return 1
        print(f"{run_date} — {len(clips)} songs\n")
        for c in clips:
            rating = c.outcome.rating if c.outcome else None
            print(f"  [{c.id:>4}] {c.title or 'Untitled':<34} "
                  f"{c.slot_type.value:<6} score {c.score_total or 0:.2f}"
                  + (f" · rated {rating}/10" if rating else " · unrated"))
        print("\nRate with:  dailyfive rate <clip_id> <1-10>"
              "   ·   undo with:  dailyfive unrate <clip_id>")
    return 0


def cmd_retro(args) -> int:
    init_db()
    result = archivist.weekly_retro(days=args.days, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_purge(args) -> int:
    """Delete stored files past their retention window."""
    from . import retention
    init_db()
    if args.usage:
        print(json.dumps(retention.usage(), indent=2, default=str))
        return 0
    result = retention.purge(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    if args.dry_run and result["removed"]:
        print(f"\n  dry run — {result['removed']} file(s) would be deleted")
    return 0


def cmd_backup(args) -> int:
    from . import backup
    init_db()
    if args.restore_hint:
        print(backup.restore_hint())
        return 0
    if args.local_only:
        path = backup.dump()
        print(f"backup written to {path} ({path.stat().st_size / 1e6:.1f} MB)")
        return 0
    key = backup.to_storage(keep_local=args.keep)
    print(f"backup stored at {key}" if key else
          "backup written locally; upload failed — see the log")
    return 0


def cmd_retype(args) -> int:
    """Correct content types on rows written before the derivation was fixed."""
    from .storage import retype_stored_files
    init_db()
    result = retype_stored_files(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    if args.dry_run and result["fixed"]:
        print(f"\n  dry run — {result['fixed']} row(s) would be corrected")
    return 0


def cmd_remeta(args) -> int:
    """Drop cover.jpg from manifests written before cover art was conditional."""
    from .storage import remeta_stored_files
    init_db()
    result = remeta_stored_files(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    if args.dry_run and result["fixed"]:
        print(f"\n  dry run — {result['fixed']} manifest(s) would be corrected")
    return 0


def cmd_strip_wav(args) -> int:
    """Remove the INFO chunk from WAVs delivered before the master dropped it."""
    from .storage import strip_wav_metadata
    init_db()
    result = strip_wav_metadata(dry_run=args.dry_run, limit=args.limit,
                                pause_s=args.pause)
    print(json.dumps(result, indent=2, default=str))
    if args.dry_run and result["stripped"]:
        print(f"\n  dry run — {result['stripped']} file(s) would be rewritten")
    return 0


def cmd_credits(args) -> int:
    from .providers.suno import SunoClient
    print(f"suno credits: {SunoClient().credits()}")
    return 0



def _init_db_with_retry(*, attempts: int = 6, fatal: bool = False) -> bool:
    """Bring the schema up, tolerating a database that is not ready yet.

    A managed cluster can refuse connections for a minute after a deploy — the
    firewall rule is still propagating, or the cluster is mid-failover. Dying
    on the first refusal produces a crashloop, and a crashlooping container is
    the one state a platform cannot show you logs for: it is gone before the
    log tail attaches. So retry, and if the database is still unreachable
    afterwards, say so loudly and carry on. The process staying up is what
    makes the failure readable — /health reports the exact database error, and
    the next request retries the connection.
    """
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            init_db()
            if attempt > 1:
                log.info("database ready after %d attempts", attempt)
            return True
        except Exception as exc:
            if attempt == attempts:
                log.error("database unreachable after %d attempts: %s", attempts, exc)
                if fatal:
                    raise
                log.error("starting anyway — /health will report the database error")
                return False
            log.warning("database not ready (attempt %d/%d): %s; retrying in %.0fs",
                        attempt, attempts, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 32.0)
    return False


def cmd_serve(args) -> int:
    import uvicorn
    _init_db_with_retry()
    cfg = settings()
    print(f"callbacks: {cfg.public_base_url}/webhooks/{'*' * 8}/generate")
    print(f"ratings:   {cfg.public_base_url}/ratings")
    uvicorn.run("dailyfive.web.app:app", host=args.host, port=args.port,
                log_level="info", proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_scheduler(args) -> int:
    """Long-running worker: runs the daily slots on its own clock."""
    from .scheduler import run_forever
    _init_db_with_retry()
    if args.dry_run:
        print("slots that would fire right now:")
        run_forever(once=True, dry_run=True)
        return 0
    print("scheduler starting — run 05:10, backup 04:30, purge 03:00 (UTC)")
    run_forever(tick_seconds=args.tick, once=args.once)
    return 0


def cmd_initdb(args) -> int:
    init_db()
    codex.current()
    print(f"database ready at {settings().database_url}")
    return 0


def cmd_migrate(args) -> int:
    """Apply pending migrations, or show where the database stands."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext

    from .db import engine

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))

    if args.action == "status":
        with engine().connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        print(f"database : {settings().database_url}")
        print(f"revision : {current or 'unmanaged (never migrated)'}")
        command.heads(cfg)
        return 0
    if args.action == "history":
        command.history(cfg, verbose=True)
        return 0
    if args.action == "down":
        command.downgrade(cfg, args.to or "-1")
        return 0

    command.upgrade(cfg, args.to or "head")
    print("migrations applied")
    return 0


def _parse_date(raw: str | None) -> date:
    if not raw:
        return date.today()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(f"bad date {raw!r} — use YYYY-MM-DD")


# ── parser ───────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dailyfive",
        description="Five finished songs a day, unattended.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("preflight", help="check everything before spending a credit")
    s.add_argument("--no-ffmpeg", action="store_true",
                   help="skip the ffmpeg check (QC and encoding will fail without it)")
    s.set_defaults(fn=cmd_preflight)

    s = sub.add_parser("run", help="run today's pipeline")
    s.add_argument("--date", help="YYYY-MM-DD (default: today)")
    s.add_argument("--no-resume", action="store_true",
                   help="fail instead of resuming an existing run for this date")
    s.add_argument("--skip-art", action="store_true", help="no cover art")
    s.add_argument("--force", action="store_true", help="run despite preflight failures")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("signals", help="test the trend feeds (free)")
    s.add_argument("--region", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_signals)

    s = sub.add_parser("personas", help="manage the recurring cast")
    s.add_argument("action", choices=["list", "bootstrap", "set"], nargs="?",
                   default="list")
    s.add_argument("--name")
    s.add_argument("--persona-id")
    s.add_argument("--persona-model", default="style_persona",
                   choices=["style_persona", "voice_persona"])
    s.add_argument("-y", "--yes", action="store_true", help="skip the spend prompt")
    s.set_defaults(fn=cmd_personas)

    s = sub.add_parser("brains", help="which brain runs which role")
    s.add_argument("--probe", action="store_true",
                   help="send a few tokens to each brain to confirm it answers")
    s.set_defaults(fn=cmd_brains)

    s = sub.add_parser("rate", help="rate a shipped song 1-10")
    s.add_argument("clip_id", type=int)
    s.add_argument("rating", type=int)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_rate)

    s = sub.add_parser("unrate", help="take a rating back, keeping its note")
    s.add_argument("clip_id", type=int)
    s.set_defaults(fn=cmd_unrate)

    s = sub.add_parser("today", help="what shipped, with clip ids")
    s.add_argument("--date")
    s.set_defaults(fn=cmd_today)

    s = sub.add_parser("status", help="recent runs and the learning signal")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("retro", help="weekly codex retrospective")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_retro)

    s = sub.add_parser("purge", help="delete stored files past their retention window")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--usage", action="store_true", help="show what the store holds")
    s.set_defaults(fn=cmd_purge)

    s = sub.add_parser("retype", help="correct stored content types in place")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_retype)

    s = sub.add_parser("remeta",
                       help="drop a cover from manifests that name one that was never made")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_remeta)

    s = sub.add_parser("backup", help="dump the database and store it")
    s.add_argument("--local-only", action="store_true")
    s.add_argument("--keep", type=int, default=7, help="local copies to retain")
    s.add_argument("--restore-hint", action="store_true")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("credits", help="Suno balance (free to check)")
    s.set_defaults(fn=cmd_credits)

    s = sub.add_parser("serve", help="run the callback and rating receiver")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("scheduler", help="run the daily slots as a worker")
    s.add_argument("--tick", type=int, default=60)
    s.add_argument("--once", action="store_true",
                   help="evaluate slots once and exit — FIRES them for real")
    s.add_argument("--dry-run", action="store_true",
                   help="report which slots are due without firing any")
    s.set_defaults(fn=cmd_scheduler)

    s = sub.add_parser("strip-wav", help="remove metadata from delivered WAVs")
    s.add_argument("--dry-run", action="store_true",
                   help="report what would change without writing")
    s.add_argument("--limit", type=int, default=0, help="stop after N files")
    s.add_argument("--pause", type=float, default=4.0,
                   help="seconds between files; the cluster is 1 vCPU")
    s.set_defaults(fn=cmd_strip_wav)

    s = sub.add_parser("init-db", help="create tables and seed the codex")
    s.set_defaults(fn=cmd_initdb)

    s = sub.add_parser("migrate", help="apply or inspect schema migrations")
    s.add_argument("action", nargs="?", default="up",
                   choices=["up", "down", "status", "history"])
    s.add_argument("--to", help="target revision (default: head)")
    s.set_defaults(fn=cmd_migrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    if getattr(args, "region", None) is None and hasattr(args, "region"):
        args.region = settings().signal_region
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except DailyFiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
