"""The daily run: sense, write, render, judge, ship.

Phases are recorded on the Run row as they complete, so a crashed process
resumes from where it stopped instead of re-spending credits on work already
done. Anything that costs money checks for existing output first.

Work is per-brief and independent up to the Producer, which is the single
barrier — ranking needs every survivor in hand at once. Below that line a slow
generation delays only itself.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from sqlalchemy import select

from . import archivist, codex, ledger
from .agents import anr, clearance, compiler, director, lyricist, producer, scout
from .config import settings
from .conductor import Conductor
from .db import init_db, session_scope
from . import llm
from .errors import BudgetExceeded, ConfigError, ProviderError
from .models import (Brief, Clip, Decision, Job, JobState, Run, RunPhase,
                     Signal, SlotType, utcnow)
from .packager import (build_meta, cover_art, encode_mp3, master_wav, slugify,
                       tag_mp3, write_lrc)
from .providers.suno import SunoClient
from .qc import FFmpegMissing, have_ffmpeg, measure, verdict
from .storage import Spaces, open_store
from .web.templates import day_page

log = logging.getLogger(__name__)


def preflight(*, require_ffmpeg: bool = True) -> list[str]:
    """Everything checkable before a credit is spent, checked at once.

    One error listing four missing things beats four consecutive runs each
    dying on the next one.
    """
    cfg = settings()
    problems: list[str] = []

    try:
        cfg.require("suno_api_key", "public_base_url")
    except ConfigError as exc:
        problems.append(str(exc))

    # Only demand keys for the brains this roster actually uses. Running the
    # whole roster on MiniMax should not require an Anthropic key.
    needed = {"anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
              "minimax": ("minimax_api_key", "MINIMAX_API_KEY"),
              "openai-compatible": ("llm_api_key", "LLM_API_KEY")}
    for provider in sorted(cfg.brains_in_use()):
        attr_env = needed.get(provider)
        if attr_env is None:
            problems.append(f"unknown LLM provider configured: {provider!r}")
        elif not getattr(cfg, attr_env[0], ""):
            roles = [r for r, b in llm.roster().items() if b.provider == provider]
            problems.append(f"{attr_env[1]} is not set — needed by "
                            f"{', '.join(roles) or provider}")
    try:
        cfg.validate_shape()
    except ConfigError as exc:
        problems.append(str(exc))

    if not cfg.webhook_secret:
        problems.append("WEBHOOK_SECRET is empty — callbacks would be world-postable")
    if require_ffmpeg and not have_ffmpeg():
        problems.append("ffmpeg/ffprobe not on PATH (apt-get install -y ffmpeg)")

    if not problems:
        try:
            open_store().check_access()
        except Exception as exc:
            problems.append(f"delivery target unreachable: {exc}")
        try:
            SunoClient().credits()
        except Exception as exc:
            problems.append(f"Suno unreachable: {exc}")

    if not (cfg.spaces_bucket and cfg.spaces_key):
        problems.append(
            "SPACES_* not set — songs will be delivered to "
            f"{cfg.work_dir / 'delivered'} instead. Fine for a first run; not a "
            "place to keep a catalogue, since Suno deletes its own copies after "
            "15 days.")

    ready = codex.current().personas_ready()
    if not ready:
        problems.append("no personas registered — run `dailyfive personas bootstrap` "
                        "or songs will render with a generic voice")
    return problems


def run_daily(run_date: date | None = None, *, resume: bool = True,
              skip_art: bool = False) -> dict:
    """One full day. Idempotent per date."""
    init_db()
    run_date = run_date or date.today()
    cfg = settings()
    cfg.validate_shape()

    run_id, phase = _open_run(run_date, resume=resume)
    # Every brain call from here on is attributed to this run.
    ledger.bind_run(run_id)
    log.info("run %d for %s starting at phase %s", run_id, run_date, phase.value)
    for role, brain in llm.roster().items():
        log.info("  brain %-10s %s", role, brain)

    try:
        if _before(phase, RunPhase.SENSED):
            phase = _phase_sense(run_id, cfg)
        if _before(phase, RunPhase.BRIEFED):
            phase = _phase_brief(run_id, cfg)
        if _before(phase, RunPhase.WRITTEN):
            phase = _phase_write(run_id, cfg)
        if _before(phase, RunPhase.SUBMITTED):
            phase = _phase_submit(run_id, cfg)
        if _before(phase, RunPhase.RENDERED):
            phase = _phase_render(run_id)
        if _before(phase, RunPhase.JUDGED):
            phase = _phase_judge(run_id, cfg)
        if _before(phase, RunPhase.SHIPPED):
            phase = _phase_ship(run_id, cfg, skip_art=skip_art)
    except BudgetExceeded as exc:
        _fail(run_id, f"budget: {exc}")
        raise
    except Exception as exc:
        log.exception("run %d failed", run_id)
        _fail(run_id, str(exc)[:2000])
        raise

    summary = archivist.record_run(run_id)
    try:
        learned = archivist.apply_learning()
        summary["codex"] = learned.get("codex_version") or learned.get("reason")
    except Exception:
        log.exception("archivist learning pass failed (run itself is fine)")

    summary["run_id"] = run_id
    summary["learning"] = archivist.learning_status()["signal"]
    summary["brains"] = {r: str(b) for r, b in llm.roster().items()}
    ledger.bind_run(None)
    log.info("run %d complete: %s", run_id, json.dumps(summary, default=str))
    return summary


# ── phases ───────────────────────────────────────────────────────────────────
def _phase_sense(run_id: int, cfg) -> RunPhase:
    sheet = scout.run(cfg.signal_region, want=10)
    with session_scope() as s:
        s.execute(Signal.__table__.delete().where(Signal.run_id == run_id))
        for t in sheet["themes"]:
            s.add(Signal(run_id=run_id, rank=t["rank"], theme=t["theme"],
                         sentiment=t["sentiment"], sources=t["sources"],
                         evidence=t["evidence"], lead=t["lead"],
                         confidence=t["confidence"]))
        run = s.get(Run, run_id)
        run.notes = {**(run.notes or {}), "scout": {
            "feeds_live": sheet["feeds_live"], "feeds_dead": sheet["feeds_dead"],
            "calibration": sheet.get("sonic_calibration", {})}}
    log.info("sense: %d themes from %d live feeds",
             len(sheet["themes"]), len(sheet["feeds_live"]))
    return _advance(run_id, RunPhase.SENSED)


def _phase_brief(run_id: int, cfg) -> RunPhase:
    cx = codex.current()
    sheet = _signal_sheet(run_id)
    specs = director.run(sheet["themes"], cx, full_n=cfg.full_briefs,
                         short_n=cfg.short_briefs,
                         calibration=sheet.get("calibration"))
    if not specs:
        raise RuntimeError("director produced no specs")

    briefs = anr.run(specs, cx)
    with session_scope() as s:
        s.execute(Brief.__table__.delete().where(Brief.run_id == run_id))
        counters = {"full": 0, "short": 0}
        for b in briefs:
            st = b.get("slot_type", "full")
            idx = counters[st]
            counters[st] += 1
            s.add(Brief(
                run_id=run_id, slot_type=SlotType(st), idx=idx,
                title=b["title"], theme=b["theme"],
                persona_id=b.get("persona_id"), persona_name=b.get("persona_name"),
                bpm=b.get("bpm"), musical_key=b.get("key"),
                song_form=b.get("song_form"), instrumentation=b.get("instrumentation"),
                vocal_gender=b.get("vocal_gender"),
                style_string=b.get("style_string"),
                negative_tags=cx.negative_tags(),
                diversity_vector={**(b.get("diversity_vector") or {}),
                                  "angle": b.get("angle"),
                                  "hook_note": b.get("hook_note"),
                                  "persona_model": b.get("persona_model")},
            ))
        s.get(Run, run_id).codex_version = cx.version
    log.info("brief: %d full, %d short", counters["full"], counters["short"])
    return _advance(run_id, RunPhase.BRIEFED)


def _phase_write(run_id: int, cfg) -> RunPhase:
    """Lyrics then clearance, per brief and independently."""
    with session_scope() as s:
        brief_ids = [b.id for b in s.execute(
            select(Brief).where(Brief.run_id == run_id).order_by(Brief.id)).scalars()]

    for bid in brief_ids:
        with session_scope() as s:
            b = s.get(Brief, bid)
            if b.lyrics:
                continue
            payload = _brief_dict(b)

        try:
            written = lyricist.run(payload)
        except ProviderError as exc:
            log.error("lyricist failed for brief %d: %s", bid, exc)
            _drop_brief(bid, f"lyricist failed: {exc}")
            continue

        checked = clearance.run(payload, written["lyrics"],
                                payload.get("style_string") or "")
        if checked["verdict"] == "reject":
            log.warning("clearance rejected brief %d: %s", bid, checked["reasons"])
            _drop_brief(bid, "clearance: " + "; ".join(checked["reasons"])[:400])
            continue

        with session_scope() as s:
            b = s.get(Brief, bid)
            b.lyrics = checked["lyrics"]
            b.lyric_hash = written["lyric_hash"]
            b.style_string = checked["style_string"]
            b.clearance = {"verdict": checked["verdict"], "reasons": checked["reasons"],
                           "severity": checked["severity"], "lint": written["lint"],
                           "draft_chosen": written["draft_chosen"]}

    surviving = _count_live_briefs(run_id)
    if surviving == 0:
        raise RuntimeError("every brief was dropped in the write phase")
    log.info("write: %d/%d briefs cleared", surviving, len(brief_ids))
    return _advance(run_id, RunPhase.WRITTEN)


def _phase_submit(run_id: int, cfg) -> RunPhase:
    client = SunoClient()
    try:
        credits = client.credits()
    except ProviderError:
        credits = None
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run.credits_start is None:
            run.credits_start = credits

    with session_scope() as s:
        briefs = s.execute(
            select(Brief).where(Brief.run_id == run_id,
                                Brief.dropped_reason.is_(None),
                                Brief.lyrics.isnot(None)).order_by(Brief.id)).scalars().all()
        existing = {j.brief_id for j in s.execute(
            select(Job).where(Job.run_id == run_id)).scalars()}
        for b in briefs:
            if b.id in existing:
                continue
            payload = compiler.compile_payload(_brief_dict(b),
                                               negative_tags=b.negative_tags)
            problems = compiler.validate(compiler.strip_internal(payload))
            if problems:
                log.error("brief %d compiled to an invalid payload: %s", b.id, problems)
                b.dropped_reason = "compile: " + "; ".join(problems)[:400]
                continue
            b.payload = payload
            # Written and committed before the request is sent — this is what
            # makes a crashed run safe to restart without paying twice.
            s.add(Job(run_id=run_id, brief_id=b.id,
                      idempotency_key=f"{run_id}:{b.id}:{b.lyric_hash or 'n'}",
                      payload=compiler.strip_internal(payload),
                      state=JobState.QUEUED))

    in_flight = Conductor(run_id, client=client).submit_all()
    log.info("submit: %d jobs in flight", len(in_flight))
    if not in_flight:
        raise RuntimeError("nothing was submitted — check credits and payloads")
    return _advance(run_id, RunPhase.SUBMITTED)


def _phase_render(run_id: int) -> RunPhase:
    cfg = settings()
    c = Conductor(run_id)
    tally = c.await_all(timeout_s=cfg.generation_timeout_s,
                        poll_interval_s=cfg.poll_interval_s)
    log.info("render: %s", json.dumps(tally))
    c.mirror_all()
    with session_scope() as s:
        n = s.execute(select(Clip).where(Clip.run_id == run_id,
                                         Clip.local_path.isnot(None))).scalars().all()
    if not n:
        raise RuntimeError("no clips were mirrored — nothing to judge")
    log.info("render: %d clips mirrored", len(n))
    return _advance(run_id, RunPhase.RENDERED)


def _phase_judge(run_id: int, cfg) -> RunPhase:
    """Measure first, then rank. Taste is only ever applied to working audio."""
    with session_scope() as s:
        rows = [(c.id, c.local_path, c.slot_type.value, c.duration_s)
                for c in s.execute(select(Clip).where(Clip.run_id == run_id)).scalars()]

    survivors: list[int] = []
    for clip_id, path, slot_type, _dur in rows:
        if not path or not Path(path).is_file():
            _mark_qc(clip_id, "fail", "no local file", {})
            continue
        try:
            metrics = measure(Path(path))
        except FFmpegMissing:
            raise
        requested = cfg.short_duration_s if slot_type == "short" else None
        v, reasons = verdict(metrics, slot_type=slot_type, requested_duration_s=requested)
        _mark_qc(clip_id, v, "; ".join(reasons), metrics.as_dict())
        if v == "pass":
            survivors.append(clip_id)
        else:
            log.info("QC cut clip %d: %s", clip_id, "; ".join(reasons))

    log.info("judge: %d/%d clips passed measurement", len(survivors), len(rows))
    if not survivors:
        raise RuntimeError("every clip failed QC — nothing is shippable today")

    candidates = _candidates(run_id, survivors)
    sheet = _signal_sheet(run_id)
    decision = producer.run(candidates, sheet, full_slots=cfg.full_slots,
                            short_slots=cfg.short_slots)

    picked = {p["clip_id"]: p for p in decision["picks"]}
    with session_scope() as s:
        for c in s.execute(select(Clip).where(Clip.run_id == run_id)).scalars():
            sc = (decision.get("scores") or {}).get(c.id) or {}
            c.score_hook = sc.get("hook")
            c.score_mix = sc.get("mix")
            c.score_trend = sc.get("trend")
            c.score_total = sc.get("total")
            if c.id in picked:
                c.shipped = True
                c.rank = picked[c.id]["rank"]
            elif c.qc_verdict == "pass":
                c.reject_reason = next(
                    (r["reason"] for r in decision["rejections"] if r["clip_id"] == c.id),
                    "not selected")
        s.execute(Decision.__table__.delete().where(Decision.run_id == run_id))
        s.add(Decision(run_id=run_id, rationale=decision.get("rationale", ""),
                       picks=decision["picks"], rejections=decision["rejections"]))
    _record_shortfall(run_id, decision["picks"], cfg)
    log.info("judge: picked %d", len(picked))
    return _advance(run_id, RunPhase.JUDGED)


def _record_shortfall(run_id: int, picks: list[dict], cfg) -> None:
    """Say loudly when a lane could not be filled.

    A run that ships four songs against a contract of five is not a failure —
    the four are real — but it is not a success either, and it must not read
    like one. The cause is always upstream: a brief dropped by Clearance, a
    Director that under-delivered, or a lane where QC took too many.
    """
    got = {"full": 0, "short": 0}
    for p in picks:
        got[p.get("slot_type", "full")] = got.get(p.get("slot_type", "full"), 0) + 1
    want = {"full": cfg.full_slots, "short": cfg.short_slots}
    gaps = {k: want[k] - got.get(k, 0) for k in want if got.get(k, 0) < want[k]}
    if not gaps:
        return

    with session_scope() as s:
        briefs = s.execute(select(Brief).where(Brief.run_id == run_id)).scalars().all()
        clips = s.execute(select(Clip).where(Clip.run_id == run_id)).scalars().all()
        causes = []
        for slot, missing in gaps.items():
            st = SlotType(slot)
            dropped = [b for b in briefs if b.slot_type is st and b.dropped_reason]
            cut = [c for c in clips if c.slot_type is st and c.qc_verdict == "fail"]
            planned = cfg.full_briefs if slot == "full" else cfg.short_briefs
            actual = len([b for b in briefs if b.slot_type is st])
            bits = [f"{missing} {slot} slot{'s' if missing > 1 else ''} unfilled"]
            if actual < planned:
                bits.append(f"the Director produced {actual} of {planned} briefs")
            if dropped:
                bits.append(f"{len(dropped)} brief(s) dropped: "
                            + "; ".join((b.dropped_reason or "")[:90] for b in dropped))
            if cut:
                bits.append(f"{len(cut)} clip(s) cut by QC")
            causes.append(" — ".join(bits))

        run = s.get(Run, run_id)
        run.notes = {**(run.notes or {}), "shortfall": {"gaps": gaps, "causes": causes}}

    for c in causes:
        log.warning("SHORTFALL: %s", c)


def _phase_ship(run_id: int, cfg, *, skip_art: bool = False) -> RunPhase:
    spaces = open_store()
    client = SunoClient()
    cond = Conductor(run_id, client=client)
    work = Path(cfg.work_dir) / str(run_id) / "master"
    work.mkdir(parents=True, exist_ok=True)

    with session_scope() as s:
        run = s.get(Run, run_id)
        run_date = run.run_date
        picks = s.execute(
            select(Clip).where(Clip.run_id == run_id, Clip.shipped.is_(True))
            .order_by(Clip.slot_type, Clip.rank)).scalars().all()
        rows = [(c.id, c.audio_id, c.local_path, c.slot_type.value, c.rank,
                 c.brief_id, c.title, dict(c.qc or {})) for c in picks]

    date_key = run_date.isoformat()
    manifest: list[dict] = []
    page_songs: list[dict] = []

    for slot_index, (clip_id, audio_id, local, slot_type, _rank, brief_id,
                     title, qc) in enumerate(rows, start=1):
        with session_scope() as s:
            brief = _brief_dict(s.get(Brief, brief_id))
            clip = s.get(Clip, clip_id)
            clip_dict = _clip_dict(clip)
            task_id = clip.job.task_id

        slug = f"{slot_index:02d}_{slugify(title or brief.get('title', ''))}"
        folder = work / slug
        folder.mkdir(parents=True, exist_ok=True)

        # True WAV from Suno; the mirrored MP3 is the fallback source.
        source = Path(local)
        cond.request_wav(clip_id)
        wav_url = cond.await_wav(clip_id, timeout_s=cfg.wav_timeout_s,
                                 poll_interval_s=max(1, cfg.poll_interval_s // 2))
        if wav_url:
            from .http import download
            try:
                raw_wav = folder / "source.wav"
                download(wav_url, raw_wav, provider="suno-wav")
                source = raw_wav
            except ProviderError as exc:
                log.warning("wav download failed for %s, mastering from mp3: %s",
                            audio_id, exc)

        from .qc import QCMetrics
        metrics = QCMetrics(**{k: v for k, v in qc.items()
                               if k in QCMetrics.__dataclass_fields__})
        master = master_wav(source, folder / "master.wav", metrics)
        mp3 = encode_mp3(master, folder / "master.mp3")

        # Re-measure the file that is actually being delivered. The gate ran on
        # the mirrored stream, whose container header Suno inflates; the
        # metadata must describe the master, not the thing QC happened to see.
        try:
            delivered = measure(master)
            if delivered.measured:
                qc = {**qc, **delivered.as_dict(), "measured_on": "master.wav"}
                with session_scope() as s:
                    clip = s.get(Clip, clip_id)
                    clip.duration_s = delivered.duration_s
                    clip.qc = qc
                clip_dict["duration_s"] = delivered.duration_s
        except Exception as exc:
            log.warning("could not re-measure the delivered master: %s", exc)

        cover = None
        if not skip_art:
            cover = cover_art(brief, folder / "cover.jpg")

        lyrics = brief.get("lyrics") or ""
        (folder / "lyrics.txt").write_text(lyrics, encoding="utf-8")
        aligned = client.timestamped_lyrics(task_id, audio_id) if task_id else []
        (folder / "lyrics.lrc").write_text(
            write_lrc(aligned, title or "Untitled", brief.get("persona_name")),
            encoding="utf-8")

        tag_mp3(mp3, title=title or "Untitled",
                artist=brief.get("persona_name") or "The Daily Five",
                album=f"The Daily Five — {date_key}", year=str(run_date.year),
                genre=(clip_dict.get("style_string") or "").split(",")[0].strip() or None,
                cover=cover, lyrics=lyrics,
                comment=f"run {run_date} · clip {clip_id} · not loudness normalised (WAV)")

        meta = build_meta({**clip_dict, "task_id": task_id}, brief, run_date,
                          slot_index, qc)
        (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        keys: dict[str, str] = {}
        for f in folder.iterdir():
            if f.name == "source.wav" or not f.is_file():
                continue
            keys[f.name] = spaces.upload(f, spaces.key_for(date_key, slug, f.name))

        with session_scope() as s:
            s.get(Clip, clip_id).spaces_key = spaces.key_for(date_key, slug)

        manifest.append({
            "slot": slot_index, "slug": slug, "clip_id": clip_id,
            "title": title, "slot_type": slot_type,
            "persona": brief.get("persona_name"),
            "bpm": brief.get("bpm"), "key": brief.get("key"),
            "duration_s": clip_dict.get("duration_s"),
            "keys": keys, "qc": qc,
            "distribution": meta["distribution"],
        })
        page_songs.append({
            "clip_id": clip_id, "title": title, "slot_type": slot_type,
            "bpm": brief.get("bpm"), "key": brief.get("key"),
            "duration_s": clip_dict.get("duration_s"),
            "persona": brief.get("persona_name"), "theme": brief.get("theme"),
            "mp3_url": spaces.signed_url(keys["master.mp3"]) if "master.mp3" in keys else None,
            "wav_url": spaces.signed_url(keys["master.wav"]) if "master.wav" in keys else None,
            "lrc_url": spaces.signed_url(keys["lyrics.lrc"]) if "lyrics.lrc" in keys else None,
        })
        log.info("shipped %s", slug)

    _write_rejects(run_id, spaces, date_key)

    status = archivist.learning_status()
    spaces.put_text(json.dumps({
        "run_date": date_key, "songs": manifest,
        "generated_at": utcnow().isoformat(),
        "learning_signal": status["signal"],
    }, indent=2, default=str), spaces.key_for(date_key, "manifest.json"))

    spaces.put_text(
        day_page(run_date, page_songs, api_base=cfg.public_base_url,
                 learning_note=f"{status['rated']}/{status['shipped']} rated so far"),
        spaces.key_for(date_key, "index.html"),
        content_type="text/html; charset=utf-8", public=cfg.spaces_public_index)

    with session_scope() as s:
        run = s.get(Run, run_id)
        try:
            run.credits_end = client.credits()
        except ProviderError:
            pass
        run.finished_at = utcnow()
    log.info("ship: %d songs delivered to %s", len(manifest),
             spaces.key_for(date_key, ""))
    return _advance(run_id, RunPhase.SHIPPED)


def _write_rejects(run_id: int, spaces: Spaces, date_key: str) -> None:
    """The ones that did not ship, with reasons. This is the reference set."""
    with session_scope() as s:
        rows = [{
            "clip_id": c.id, "title": c.title, "slot_type": c.slot_type.value,
            "qc_verdict": c.qc_verdict, "qc_reason": c.qc_reason,
            "score_total": c.score_total, "reject_reason": c.reject_reason,
            "style_string": c.style_string, "local_mp3": bool(c.local_path),
        } for c in s.execute(
            select(Clip).where(Clip.run_id == run_id,
                               Clip.shipped.is_(False))).scalars()]
    if rows:
        spaces.put_text(json.dumps(rows, indent=2, default=str),
                        spaces.key_for(date_key, "_rejected", "rejects.json"))


# ── helpers ──────────────────────────────────────────────────────────────────
_PHASE_ORDER = list(RunPhase)


def _before(current: RunPhase, target: RunPhase) -> bool:
    if current == RunPhase.FAILED:
        return True
    return _PHASE_ORDER.index(current) < _PHASE_ORDER.index(target)


def _advance(run_id: int, phase: RunPhase) -> RunPhase:
    with session_scope() as s:
        s.get(Run, run_id).phase = phase
    return phase


def _fail(run_id: int, error: str) -> None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        run.error = error
        if run.phase != RunPhase.SHIPPED:
            run.phase = RunPhase.FAILED


def _open_run(run_date: date, *, resume: bool) -> tuple[int, RunPhase]:
    with session_scope() as s:
        run = s.execute(select(Run).where(Run.run_date == run_date)).scalar_one_or_none()
        if run is None:
            run = Run(run_date=run_date, phase=RunPhase.CREATED)
            s.add(run)
            s.flush()
            return run.id, RunPhase.CREATED
        if not resume:
            raise RuntimeError(
                f"a run already exists for {run_date} at phase {run.phase.value}; "
                "pass resume=True to continue it")
        if run.phase == RunPhase.FAILED:
            # A failed run restarts from the last phase it actually completed,
            # which the tables themselves record.
            resumed = _infer_phase(s, run.id)
            log.info("resuming failed run from %s", resumed.value)
            run.phase = resumed
            run.error = None
        return run.id, run.phase


def _infer_phase(s, run_id: int) -> RunPhase:
    """Derive the last completed phase from what is actually in the database."""
    if s.execute(select(Clip).where(Clip.run_id == run_id,
                                    Clip.shipped.is_(True))).first():
        return RunPhase.JUDGED
    if s.execute(select(Clip).where(Clip.run_id == run_id,
                                    Clip.qc_verdict.isnot(None))).first():
        return RunPhase.RENDERED
    if s.execute(select(Clip).where(Clip.run_id == run_id,
                                    Clip.local_path.isnot(None))).first():
        return RunPhase.RENDERED
    if s.execute(select(Job).where(Job.run_id == run_id,
                                  Job.task_id.isnot(None))).first():
        return RunPhase.SUBMITTED
    if s.execute(select(Brief).where(Brief.run_id == run_id,
                                     Brief.lyrics.isnot(None))).first():
        return RunPhase.WRITTEN
    if s.execute(select(Brief).where(Brief.run_id == run_id)).first():
        return RunPhase.BRIEFED
    if s.execute(select(Signal).where(Signal.run_id == run_id)).first():
        return RunPhase.SENSED
    return RunPhase.CREATED


def _signal_sheet(run_id: int) -> dict:
    with session_scope() as s:
        run = s.get(Run, run_id)
        rows = s.execute(select(Signal).where(Signal.run_id == run_id)
                         .order_by(Signal.rank)).scalars().all()
        themes = [{"rank": r.rank, "theme": r.theme, "sentiment": r.sentiment,
                   "evidence": r.evidence, "sources": r.sources, "lead": r.lead,
                   "confidence": r.confidence} for r in rows]
        notes = (run.notes or {}).get("scout", {})
    return {"themes": themes, "calibration": notes.get("calibration", {}),
            "feeds_live": notes.get("feeds_live", [])}


def _brief_dict(b: Brief) -> dict:
    dv = dict(b.diversity_vector or {})
    return {
        "id": b.id, "title": b.title, "theme": b.theme,
        "slot_type": b.slot_type.value, "bpm": b.bpm, "key": b.musical_key,
        "musical_key": b.musical_key, "song_form": b.song_form,
        "instrumentation": b.instrumentation, "vocal_gender": b.vocal_gender,
        "style_string": b.style_string, "negative_tags": b.negative_tags,
        "persona_id": b.persona_id, "persona_name": b.persona_name,
        "persona_model": dv.get("persona_model"),
        "angle": dv.get("angle"), "hook_note": dv.get("hook_note"),
        "diversity_vector": dv, "lyrics": b.lyrics, "lyric_hash": b.lyric_hash,
    }


def _clip_dict(c: Clip) -> dict:
    return {
        "clip_id": c.id, "audio_id": c.audio_id, "title": c.title,
        "slot_type": c.slot_type.value, "duration_s": c.duration_s,
        "style_string": c.style_string, "negative_tags": c.negative_tags,
        "model": c.model, "vocal_gender": c.vocal_gender,
        "style_weight": c.style_weight, "weirdness": c.weirdness,
        "audio_weight": c.audio_weight, "tags": c.tags,
        "score_hook": c.score_hook, "score_mix": c.score_mix,
        "score_trend": c.score_trend, "score_total": c.score_total,
    }


def _candidates(run_id: int, survivor_ids: list[int]) -> list[dict]:
    with session_scope() as s:
        out = []
        for c in s.execute(select(Clip).where(Clip.id.in_(survivor_ids))).scalars():
            b = s.get(Brief, c.brief_id)
            out.append({**_clip_dict(c), "qc": dict(c.qc or {}),
                        "theme": c.theme, "song_form": c.song_form,
                        "lyrics": b.lyrics if b else None,
                        "hook_note": (b.diversity_vector or {}).get("hook_note") if b else None,
                        "diversity_vector": b.diversity_vector if b else {},
                        "requested_duration_s": settings().short_duration_s
                        if c.slot_type == SlotType.SHORT else None})
        return out


def _mark_qc(clip_id: int, v: str, reason: str, metrics: dict) -> None:
    with session_scope() as s:
        c = s.get(Clip, clip_id)
        c.qc_verdict = v
        c.qc_reason = reason or None
        c.qc = metrics


def _drop_brief(brief_id: int, reason: str) -> None:
    with session_scope() as s:
        s.get(Brief, brief_id).dropped_reason = reason


def _count_live_briefs(run_id: int) -> int:
    with session_scope() as s:
        return len(s.execute(
            select(Brief).where(Brief.run_id == run_id,
                                Brief.dropped_reason.is_(None),
                                Brief.lyrics.isnot(None))).scalars().all())
