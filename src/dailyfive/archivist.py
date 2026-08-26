"""Archivist — the reason day 30 differs from day 1.

The daily half is plain code: it writes one row per clip, shipped or not, and
recomputes the aggregates the Director reads. There is no model in that path
because there is nothing to interpret — it is arithmetic over rows.

The weekly retro is the one place a model earns its keep here, proposing codex
edits from patterns the aggregates surface. It proposes; it never writes
directly. Every change lands as a new codex version with a rationale, so a
regression can be traced to the edit that caused it.

The honest caveat, stated in code as well as prose: until ``Outcome.rating`` is
populated, every "score" below is the Producer's own opinion fed back to itself.
That is better than nothing and worse than evidence, and
:func:`learning_status` reports which one you are currently running on.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select

from .codex import current as current_codex
from .codex import save_new_version
from .db import session_scope
from .errors import ProviderError
from .models import Clip, Outcome, Run, SlotType
from .agents.base import ask_json

log = logging.getLogger(__name__)

MIN_OBSERVATIONS = 4          # below this, an average is noise
RETRO_MIN_RUNS = 5


def record_run(run_id: int) -> dict:
    """Close the books on a run. Returns a summary for the log."""
    with session_scope() as s:
        run = s.get(Run, run_id)
        clips = s.execute(select(Clip).where(Clip.run_id == run_id)).scalars().all()
        summary = {
            "run_date": run.run_date.isoformat() if run else None,
            "clips": len(clips),
            "shipped": sum(1 for c in clips if c.shipped),
            "qc_failed": sum(1 for c in clips if c.qc_verdict == "fail"),
            "credits_spent": run.credits_spent if run else None,
        }
    log.info("archivist: %s", json.dumps(summary))
    return summary


def rate(clip_id: int, rating: int, note: str | None = None) -> None:
    """Record your rating. This is the field the pipeline cannot fill in."""
    from .models import utcnow
    rating = max(1, min(10, int(rating)))
    with session_scope() as s:
        row = s.execute(select(Outcome).where(Outcome.clip_id == clip_id)).scalar_one_or_none()
        if row is None:
            row = Outcome(clip_id=clip_id)
            s.add(row)
        row.rating = rating
        if note:
            row.note = note[:2000]
        row.rated_at = utcnow()
    log.info("clip %d rated %d/10", clip_id, rating)


def learning_status() -> dict:
    """Which signal the loop is actually optimising against, stated plainly."""
    with session_scope() as s:
        total = s.execute(select(func.count(Clip.id))).scalar() or 0
        shipped = s.execute(
            select(func.count(Clip.id)).where(Clip.shipped.is_(True))).scalar() or 0
        rated = s.execute(
            select(func.count(Outcome.id)).where(Outcome.rating.isnot(None))).scalar() or 0
        runs = s.execute(select(func.count(Run.id))).scalar() or 0

    coverage = (rated / shipped) if shipped else 0.0
    if rated == 0:
        signal = ("producer-only — no ratings recorded yet, so the loop is "
                  "optimising for the Producer agent's opinion")
    elif coverage < 0.5:
        signal = (f"mixed — {rated} of {shipped} shipped songs rated "
                  f"({coverage:.0%}); ratings are starting to dominate")
    else:
        signal = f"rating-led — {coverage:.0%} of shipped songs carry your rating"

    return {"runs": runs, "clips": total, "shipped": shipped,
            "rated": rated, "coverage": round(coverage, 3), "signal": signal}


def _clip_value(clip: Clip, outcome: Outcome | None) -> float:
    """One number per clip on a 0-10 scale.

    Your rating wins outright where it exists. Where it does not, the Producer's
    score stands in, damped toward the midpoint so an unrated clip never
    outweighs a rated one.
    """
    if outcome and outcome.rating is not None:
        return float(outcome.rating)
    if clip.score_total is not None:
        return 5.0 + (float(clip.score_total) - 5.0) * 0.6
    return 3.0 if clip.qc_verdict == "fail" else 5.0


def aggregate(days: int = 60) -> dict:
    """Recompute what the Director reads. Pure arithmetic over rows."""
    cutoff = date.today() - timedelta(days=days)
    with session_scope() as s:
        rows = s.execute(
            select(Clip, Outcome)
            .join(Run, Clip.run_id == Run.id)
            .outerjoin(Outcome, Outcome.clip_id == Clip.id)
            .where(Run.run_date >= cutoff)).all()

        style_vals: dict[str, list[float]] = defaultdict(list)
        bpm_vals: dict[str, list[float]] = defaultdict(list)
        persona_vals: dict[str, list[float]] = defaultdict(list)
        fail_reasons: dict[str, int] = defaultdict(int)
        n = 0

        for clip, outcome in rows:
            n += 1
            value = _clip_value(clip, outcome)
            for token in _style_tokens(clip.style_string):
                style_vals[token].append(value)
            if clip.bpm_target:
                bpm_vals[_bpm_bucket(clip.bpm_target)].append(value)
            if clip.persona_id:
                persona_vals[clip.persona_id].append(value)
            if clip.qc_verdict == "fail" and clip.qc_reason:
                fail_reasons[_first_reason(clip.qc_reason)] += 1

    return {
        "observations": n,
        "style_scores": _means(style_vals),
        "bpm_scores": _means(bpm_vals),
        "persona_scores": _means(persona_vals),
        "qc_failure_reasons": dict(sorted(fail_reasons.items(),
                                          key=lambda kv: -kv[1])[:10]),
        "avoid": _avoid_list(style_vals),
    }


def apply_learning(*, days: int = 60, dry_run: bool = False) -> dict:
    """Fold the aggregates into a new codex version."""
    stats = aggregate(days)
    if stats["observations"] < MIN_OBSERVATIONS:
        log.info("archivist: %d observations, need %d before editing the codex",
                 stats["observations"], MIN_OBSERVATIONS)
        return {"changed": False, "reason": "not enough observations", **stats}

    cx = current_codex()
    body = json.loads(json.dumps(cx.body))
    learned = body.setdefault("learned", {})
    before = json.dumps(learned, sort_keys=True)

    learned["style_scores"] = stats["style_scores"]
    learned["bpm_scores"] = stats["bpm_scores"]
    learned["avoid"] = stats["avoid"]
    learned["observations"] = stats["observations"]
    learned["qc_failure_reasons"] = stats["qc_failure_reasons"]

    if json.dumps(learned, sort_keys=True) == before:
        return {"changed": False, "reason": "no change in aggregates", **stats}
    if dry_run:
        return {"changed": False, "reason": "dry run", "would_write": learned, **stats}

    status = learning_status()
    version = save_new_version(
        body, cx.personas,
        diff=f"learned block refreshed from {stats['observations']} clips",
        rationale=f"daily aggregate over {days}d; signal is {status['signal']}")
    return {"changed": True, "codex_version": version, **stats}


RETRO_SYSTEM = """You are running the weekly retrospective for an automated music \
studio. You see aggregate outcomes across recent runs and propose specific, \
checkable edits to the Style Codex.

Propose only what the data supports. An average over three clips is noise — say \
so rather than inventing a trend. If nothing in the data justifies a change, \
returning an empty list is the correct answer and a valued one.

Every proposal must be concrete enough to verify next week: "raise the midtempo \
band floor from 79 to 84 BPM" is checkable, "lean more melodic" is not.

Be explicit about which signal you are reading. If most clips carry no human \
rating, you are reading the Producer agent's own scores fed back to itself — \
that is weak evidence and your confidence should reflect it."""

RETRO_SCHEMA = """{
  "notes": ["observation worth carrying in the codex, <= 200 chars"],
  "tempo_band_edits": {"band_name": [low, high]},
  "avoid_additions": ["descriptor to stop using"],
  "palette_additions": ["new instrumentation palette, <= 120 chars"],
  "confidence": "low|medium|high",
  "evidence_note": "what the data does and does not support, <= 300 chars"
}"""


def weekly_retro(*, days: int = 14, dry_run: bool = False) -> dict:
    """Model-proposed codex edits. Proposes only; never writes directly."""
    with session_scope() as s:
        runs = s.execute(
            select(func.count(Run.id)).where(
                Run.run_date >= date.today() - timedelta(days=days))).scalar() or 0
    if runs < RETRO_MIN_RUNS:
        return {"changed": False,
                "reason": f"{runs} runs in {days}d, need {RETRO_MIN_RUNS}"}

    stats = aggregate(days)
    status = learning_status()
    cx = current_codex()

    try:
        result = ask_json(
            "retro", RETRO_SYSTEM,
            f"Codex v{cx.version}:\n{cx.brief_context()}\n\n"
            f"Aggregates over the last {days} days:\n"
            f"{json.dumps(stats, ensure_ascii=False)}\n\n"
            f"Learning signal in use: {status['signal']}\n"
            f"({status['rated']} of {status['shipped']} shipped songs rated)",
            schema_hint=RETRO_SCHEMA, max_tokens=3000, temperature=0.4,
            label="weekly-retro")
    except ProviderError as exc:
        return {"changed": False, "reason": f"retro model call failed: {exc}"}

    body = json.loads(json.dumps(cx.body))
    changes: list[str] = []

    for band, rng in (result.get("tempo_band_edits") or {}).items():
        if band in body.get("tempo_bands", {}) and isinstance(rng, list) and len(rng) == 2:
            try:
                lo, hi = int(rng[0]), int(rng[1])
            except (TypeError, ValueError):
                continue
            if 40 <= lo < hi <= 220:
                body["tempo_bands"][band] = [lo, hi]
                changes.append(f"tempo band {band} -> {lo}-{hi}")

    learned = body.setdefault("learned", {})
    avoid = set(learned.get("avoid") or [])
    for a in (result.get("avoid_additions") or [])[:8]:
        if isinstance(a, str) and a.strip():
            avoid.add(a.strip()[:60])
    if avoid != set(learned.get("avoid") or []):
        learned["avoid"] = sorted(avoid)
        changes.append(f"avoid list now {len(avoid)} entries")

    palettes = list(body.get("instrumentation_palettes") or [])
    for p in (result.get("palette_additions") or [])[:3]:
        if isinstance(p, str) and p.strip() and p.strip() not in palettes:
            palettes.append(p.strip()[:160])
            changes.append("palette added")
    body["instrumentation_palettes"] = palettes[:12]

    notes = [str(n)[:200] for n in (result.get("notes") or [])][:6]
    if notes:
        learned["notes"] = (learned.get("notes") or [])[-10:] + notes
        changes.append(f"{len(notes)} notes")

    if not changes:
        return {"changed": False, "reason": "retro proposed no changes",
                "evidence_note": result.get("evidence_note")}
    if dry_run:
        return {"changed": False, "reason": "dry run", "would_change": changes,
                "evidence_note": result.get("evidence_note")}

    version = save_new_version(
        body, cx.personas, diff="; ".join(changes),
        rationale=f"weekly retro ({result.get('confidence', 'low')} confidence): "
                  f"{result.get('evidence_note', '')}"[:900])
    return {"changed": True, "codex_version": version, "changes": changes,
            "confidence": result.get("confidence"),
            "evidence_note": result.get("evidence_note")}


# ── helpers ──────────────────────────────────────────────────────────────────
def _style_tokens(style: str | None) -> list[str]:
    if not style:
        return []
    out = []
    for part in style.split(","):
        tok = part.strip().lower()
        if 2 < len(tok) <= 40:
            out.append(tok)
    return out[:24]


def _bpm_bucket(bpm: int) -> str:
    for name, (lo, hi) in (("ballad", (0, 78)), ("midtempo", (79, 104)),
                           ("uptempo", (105, 128)), ("club", (129, 999))):
        if lo <= bpm <= hi:
            return name
    return "unknown"


def _first_reason(reason: str) -> str:
    """Group failures by kind, not by exact numbers, or every one is unique."""
    head = reason.split(";")[0].strip().lower()
    for key in ("clipping", "clipped", "silence", "dead air", "duration",
                "near-silent", "crushed", "dc offset", "measured"):
        if key in head:
            return key
    return head[:40]


def _means(vals: dict[str, list[float]]) -> dict[str, float]:
    """Only report an average once there is enough behind it to mean something."""
    return {k: round(statistics.fmean(v), 2)
            for k, v in vals.items() if len(v) >= MIN_OBSERVATIONS}


def _avoid_list(style_vals: dict[str, list[float]]) -> list[str]:
    return sorted(
        k for k, v in style_vals.items()
        if len(v) >= MIN_OBSERVATIONS and statistics.fmean(v) < 4.0)[:12]
