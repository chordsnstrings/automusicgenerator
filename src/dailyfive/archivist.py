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

from . import genres
from .codex import current as current_codex
from .codex import is_negation, save_new_version
from .db import session_scope
from .errors import ProviderError
from .models import Clip, Outcome, Publication, Run, SlotType
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


def unrate(clip_id: int) -> bool:
    """Take back a rating without taking back the note that came with it.

    The row survives and only ``rating`` and ``rated_at`` are cleared. Deleting
    it would be indistinguishable to every reader — all five filter on rating
    IS NOT NULL — but ``Outcome`` also holds the note you typed, and ``plays``
    and ``saves``, the slots reserved for what reality adds later. The stated
    mistake is a mis-tap on a 1-10 widget; the prose is not part of it.

    Returns True when a rating was actually cleared, so a caller can say
    "cleared" rather than "was already unrated" without a second query. A
    missing row or an already-null rating is a no-op and not an error, because
    a double-tap on a clear control must not fail.

    ``rating = 0`` is deliberately not overloaded as "clear": both write paths
    reject it as out of range, and making it mean something would turn a
    documented rejection into a lie.
    """
    with session_scope() as s:
        row = s.execute(select(Outcome).where(Outcome.clip_id == clip_id)).scalar_one_or_none()
        if row is None or row.rating is None:
            return False
        row.rating = None
        row.rated_at = None
    log.info("clip %d rating cleared", clip_id)
    return True


def learning_status() -> dict:
    """Which signal the loop is actually optimising against, stated plainly."""
    with session_scope() as s:
        total = s.execute(select(func.count(Clip.id))).scalar() or 0
        shipped = s.execute(
            select(func.count(Clip.id)).where(Clip.shipped.is_(True))).scalar() or 0
        rated = s.execute(
            select(func.count(Outcome.id)).where(Outcome.rating.isnot(None))).scalar() or 0
        runs = s.execute(select(func.count(Run.id))).scalar() or 0

    measured = len(audience_scale())

    coverage = (rated / shipped) if shipped else 0.0
    # Stated in the order the value function actually applies them, so this
    # sentence stays true as the studio moves down the list. Views first,
    # because they are what the studio is for; the rating next, because it is a
    # forecast of the same thing and a good one; the Producer's own score last,
    # which is the case where the loop is grading its own homework.
    if measured >= 3:
        signal = (f"audience-led — {measured} published songs carry view counts, "
                  f"ranked against each other at {int(VIEWS_WEIGHT * 100)}% of "
                  f"the weight where a rating exists too")
    elif rated == 0:
        signal = ("producer-only — no ratings and no view counts, so the loop is "
                  "optimising for the Producer agent's opinion")
    elif coverage < 0.5:
        signal = (f"mixed — {rated} of {shipped} shipped songs rated "
                  f"({coverage:.0%}); ratings are starting to dominate")
    else:
        signal = f"rating-led — {coverage:.0%} of shipped songs carry your rating"

    return {"runs": runs, "clips": total, "shipped": shipped,
            "rated": rated, "measured": measured,
            "coverage": round(coverage, 3), "signal": signal}


# How much of a clip's value comes from what an audience did, when that is
# known at all. The rest is your rating. Views lead because the studio's whole
# premise is that a song succeeds by being watched and reused, and a rating is a
# forecast of that; but the forecast is not noise — you hear things a view count
# cannot express, and a song that travelled for reasons unrelated to the music
# is a real thing that happens.
VIEWS_WEIGHT = 0.7


def audience_scale(days: int = 60) -> dict[int, float]:
    """Each published clip's view count as a 0-10 score, ranked against its peers.

    Ranked rather than scaled against a constant, and that is the whole design.
    A view count has no natural ceiling and no meaning on its own: 400 views is
    a hit on a channel with sixty subscribers and a failure on one with a
    million, and any fixed divisor would encode today's channel size into a
    codex that is meant to outlast it. A percentile answers the only question
    the learning loop actually asks — did this one do better than the others —
    and it keeps answering it as the channel grows.

    Views are summed across platforms rather than compared. A song on TikTok and
    on YouTube is one song reaching two audiences, and the sum is how much reach
    it got; treating them as two observations would count every published song
    twice and quietly double the weight of whatever is published to both.

    Fewer than three published clips returns nothing. A percentile over two
    points is a coin toss dressed as a measurement, and it would arrive with the
    full authority of a number.
    """
    cutoff = date.today() - timedelta(days=days)
    with session_scope() as s:
        rows = s.execute(
            select(Publication.clip_id, func.sum(Publication.views))
            .join(Clip, Clip.id == Publication.clip_id)
            .join(Run, Clip.run_id == Run.id)
            .where(Run.run_date >= cutoff, Publication.views.isnot(None))
            .group_by(Publication.clip_id)).all()

    totals = [(cid, int(v or 0)) for cid, v in rows]
    if len(totals) < 3:
        return {}

    ordered = sorted(totals, key=lambda kv: kv[1])
    n = len(ordered)
    out: dict[int, float] = {}
    for i, (clip_id, _views) in enumerate(ordered):
        out[clip_id] = round(10.0 * i / (n - 1), 3)

    # Ties must not be broken by row order — two songs on the same view count
    # are the same observation, and giving one of them a better score than the
    # other would be the enumeration index leaking into the codex.
    by_views: dict[int, list[float]] = defaultdict(list)
    for clip_id, views in ordered:
        by_views[views].append(out[clip_id])
    means = {v: sum(scores) / len(scores) for v, scores in by_views.items()}
    for clip_id, views in ordered:
        out[clip_id] = round(means[views], 3)
    return out


def _clip_value(clip: Clip, outcome: Outcome | None,
                audience: float | None = None) -> float:
    """One number per clip on a 0-10 scale.

    What an audience did leads where it is known, blended with your rating where
    that exists too. Where neither does, the Producer's score stands in, damped
    toward the midpoint so an unrated clip never outweighs a rated one.
    """
    rating = float(outcome.rating) if outcome and outcome.rating is not None else None
    if audience is not None and rating is not None:
        return audience * VIEWS_WEIGHT + rating * (1.0 - VIEWS_WEIGHT)
    if audience is not None:
        return audience
    if rating is not None:
        return rating
    if clip.score_total is not None:
        return 5.0 + (float(clip.score_total) - 5.0) * 0.6
    return 3.0 if clip.qc_verdict == "fail" else 5.0


def aggregate(days: int = 60) -> dict:
    """Recompute what the Director reads. Pure arithmetic over rows."""
    cutoff = date.today() - timedelta(days=days)
    audience = audience_scale(days)
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
            value = _clip_value(clip, outcome, audience.get(clip.id))
            for token in _style_tokens(clip.style_string):
                style_vals[token].append(value)
            if clip.bpm_target:
                bpm_vals[_bpm_bucket(clip.bpm_target)].append(value)
            if clip.persona_id:
                persona_vals[clip.persona_id].append(value)
            if clip.qc_verdict == "fail" and clip.qc_reason:
                fail_reasons[_first_reason(clip.qc_reason)] += 1

    # Outside the session above, and outside the window too. genres.scores()
    # opens its own session and answers a different question: everything else
    # here is a mean over CLIPS inside `days`, and genre is a mean over distinct
    # rated BRIEFS with a half-life instead of a cutoff. Two clips of a pair
    # carry identical brief-derived fields, so counting them as two samples is
    # exactly how "no synths" and "no pads" reached the codex off two decisions
    # — and a controlled label repeats by design, which would make that error
    # worse here rather than milder.
    genre = genres.scores()

    return {
        "observations": n,
        "published_with_views": len(audience),
        "style_scores": _means(style_vals),
        "bpm_scores": _means(bpm_vals),
        "persona_scores": _means(persona_vals),
        "genre_scores": _genre_means(genre["families"]),
        "subgenre_scores": _genre_means(genre["specifics"]),
        "genre_rated_briefs": genre["rated_briefs"],
        "genre_unlabelled_briefs": genre["unlabelled_briefs"],
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
    # Gated in genres.scores(), which reports no mean at all below
    # GENRE_MIN_RATED, so a family under the bar writes nothing rather than
    # writing a number the Director would be told beats its priors. The gate is
    # there and not here for the same reason _means() holds MIN_OBSERVATIONS:
    # one place decides what counts as enough, and the console reads the same
    # answer this does.
    learned["genre_scores"] = stats["genre_scores"]
    learned["subgenre_scores"] = stats["subgenre_scores"]
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
that is weak evidence and your confidence should reflect it.

The genre vocabulary is closed and it is code, not codex — you cannot edit it \
and nothing you write here will. If the evidence says a term is missing (the \
off-vocabulary count is high, or a family's briefs keep splitting into two \
sounds that should be scored apart), say so in genre_vocabulary_note and name \
the label you would add. It is read as a proposal for a person to make as a \
code change, and an empty string is the right answer nearly every week."""

RETRO_SCHEMA = """{
  "notes": ["observation worth carrying in the codex, <= 200 chars"],
  "tempo_band_edits": {"band_name": [low, high]},
  "avoid_additions": ["descriptor to stop using"],
  "palette_additions": ["new instrumentation palette, <= 120 chars"],
  "genre_vocabulary_note": "a missing genre label and why, <= 200 chars; usually empty",
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

    # Its own key rather than appended to learned["notes"], and the divergence
    # is deliberate: brief_context() renders notes straight into the Music
    # Director's prompt, so a note saying "add afrobeats" would be an
    # instruction to write a word the vocabulary does not carry. normalise()
    # would null it, the brief would lose its genre, and a day of evidence
    # would be spent proving the retro can reach the Director. The proposal is
    # for a person; it renders on /genres and goes nowhere near a prompt.
    proposal = str(result.get("genre_vocabulary_note") or "").strip()[:200]
    if proposal and proposal != (learned.get("genre_vocabulary_note") or ""):
        learned["genre_vocabulary_note"] = proposal
        changes.append("genre vocabulary note")

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
    """The scoreable fragments of a style string.

    Negations are dropped rather than scored — see the note beside
    ``codex.is_negation``. A fragment saying what is absent cannot be credited
    with an outcome in either direction, and both of the two entries the live
    learned table ever accumulated were negations.
    """
    if not style:
        return []
    out = []
    for part in style.split(","):
        tok = part.strip().lower()
        if 2 < len(tok) <= 40 and not is_negation(tok):
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


def _genre_means(rows: dict[str, dict]) -> dict[str, dict]:
    """The genre rows that have cleared the bar, each keeping its sample count.

    The n is carried into the codex rather than dropped on the way in. The live
    codex reached v3 rendering "no synths 6.11" with no count anywhere on the
    row, and that is how an average over two decisions came to read as a
    finding; a genre label repeats by construction, so the same number here
    would look solid sooner and be wrong for longer.
    """
    return {label: {"mean": row["taste"], "n": row["rated_n"]}
            for label, row in sorted(rows.items())
            if row.get("taste") is not None}


def _means(vals: dict[str, list[float]]) -> dict[str, float]:
    """Only report an average once there is enough behind it to mean something."""
    return {k: round(statistics.fmean(v), 2)
            for k, v in vals.items() if len(v) >= MIN_OBSERVATIONS}


def _avoid_list(style_vals: dict[str, list[float]]) -> list[str]:
    return sorted(
        k for k, v in style_vals.items()
        if len(v) >= MIN_OBSERVATIONS and statistics.fmean(v) < 4.0)[:12]
