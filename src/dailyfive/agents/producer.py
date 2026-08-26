"""Producer — three independent scoring passes, then a slot-typed selection.

Two design choices matter here.

The scoring passes are separate calls, not three fields in one response. Asked
for all three at once, a model anchors: a clip it likes gets high marks on every
axis. Asked separately, hook strength and mix quality genuinely diverge, which is
what makes the ranking informative.

Selection is per slot type. Full-length and short-form candidates never compete,
because a 45-second loop cannot fill a 3-minute slot no matter how good it is.
Today only the full-length lane has slots — seven briefs contest five — so the
per-type machinery decides nothing visible. It stays because the lane is a
setting: the day SHORT_SLOTS goes above zero it is what stops a loop being
shipped as a song.

The Producer only ever sees clips that already passed measurement. Taste is
applied to working audio, never used to decide whether audio is working.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

from ..errors import ProviderError
from .base import ask_json, clamp

log = logging.getLogger(__name__)

AXES = {
    "hook": """Score each candidate on HOOK STRENGTH.

The question is narrow: in the first seven seconds, is there something a \
listener would still be able to hum tomorrow? A strong hook is memorable on one \
listen and survives being heard through a phone speaker in a noisy room.

You are scoring the hook, not the song. A beautiful arrangement with a forgettable \
top line scores low here. Judge from the title, the lyric's opening and chorus, \
and the declared arrangement.""",

    "mix": """Score each candidate on VOCAL AND MIX QUALITY as evidenced by the \
measurements you are given.

The QC metrics are objective and already passed threshold, so you are ranking \
within "acceptable", not deciding pass or fail. A track sitting comfortably in \
range scores higher than one that scraped through. Consider: how close to the \
loudness target, how much true-peak headroom, how tight the duration is against \
what was asked for, how little dead air.

Do not speculate about anything the metrics do not show. You cannot hear these.""",

    "trend": """Score each candidate on FIT TO TODAY'S SIGNAL SHEET.

How directly does this song serve one of today's ranked themes, and how leading \
was the evidence behind that theme? A song that nails a theme backed by leading \
evidence scores highest. A well-made song about nothing in particular scores in \
the middle. A song chasing a lagging theme scores low — that moment already \
happened.""",
}

SCHEMA = """{"scores": [{"clip_id": 0, "score": 0.0, "why": "<= 120 chars"}]}"""

FINAL_SYSTEM = """You are the Producer making the final call on today's release.

You have three independent scores per candidate and the measurements behind them. \
Pick the best candidate for each slot, and write down why — including why each \
rejected candidate lost, because those reasons are what the studio learns from.

Prefer variety across the shipped set where scores are close. Two excellent songs \
that sound like each other serve the day worse than one excellent and one very \
good song that do not."""

FINAL_SCHEMA = """{
  "picks": [{"clip_id": 0, "slot_type": "SLOT_TYPES", "rank": 1, "why": "<= 200 chars"}],
  "rejections": [{"clip_id": 0, "reason": "specific, <= 200 chars"}],
  "rationale": "how this set holds together as a day's release, <= 500 chars"
}"""


def run(candidates: list[dict], signals: dict, *, full_slots: int,
        short_slots: int) -> dict:
    """Score, then select. ``candidates`` are QC survivors only."""
    if not candidates:
        return {"picks": [], "rejections": [], "rationale": "no candidates survived QC",
                "scores": {}}

    scores: dict[int, dict[str, float]] = defaultdict(dict)
    reasons: dict[int, dict[str, str]] = defaultdict(dict)

    for axis, system in AXES.items():
        try:
            got = _score_axis(axis, system, candidates, signals)
        except ProviderError as exc:
            log.warning("producer axis %s failed (%s) — defaulting to 5.0", axis, exc)
            got = {c["clip_id"]: (5.0, "axis unavailable") for c in candidates}
        for cid, (val, why) in got.items():
            scores[cid][axis] = val
            reasons[cid][axis] = why

    for c in candidates:
        s = scores[c["clip_id"]]
        c["score_hook"] = s.get("hook", 5.0)
        c["score_mix"] = s.get("mix", 5.0)
        c["score_trend"] = s.get("trend", 5.0)
        # Hook is what a listener notices first, so it carries the most weight.
        c["score_total"] = round(
            0.45 * c["score_hook"] + 0.25 * c["score_mix"] + 0.30 * c["score_trend"], 3)

    selection = _select(candidates, signals, full_slots=full_slots, short_slots=short_slots)
    selection["scores"] = {c["clip_id"]: {
        "hook": c["score_hook"], "mix": c["score_mix"],
        "trend": c["score_trend"], "total": c["score_total"],
        "why": reasons[c["clip_id"]],
    } for c in candidates}
    return selection


def _score_axis(axis: str, system: str, candidates: list[dict],
                signals: dict) -> dict[int, tuple[float, str]]:
    payload = [_view(c, axis) for c in candidates]
    user = (
        f"Score every candidate from 0 to 10 on this axis alone.\n\n"
        f"Candidates:\n{json.dumps(payload, ensure_ascii=False)[:40000]}\n\n"
        + (f"Today's signal sheet:\n{json.dumps(signals.get('themes', [])[:10], ensure_ascii=False)}"
           if axis == "trend" else "")
    )
    result = ask_json("producer", system, user, schema_hint=SCHEMA,
                      max_tokens=3000, temperature=0.4, label=f"score:{axis}")
    out: dict[int, tuple[float, str]] = {}
    for row in result.get("scores") or []:
        try:
            cid = int(row["clip_id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[cid] = (clamp(row.get("score"), 0.0, 10.0, 5.0), str(row.get("why", ""))[:160])
    for c in candidates:
        out.setdefault(c["clip_id"], (5.0, "not scored"))
    return out


def _view(c: dict, axis: str) -> dict:
    """Show each axis only what it should judge on.

    The mix scorer seeing the lyric would let a good lyric inflate a mix score,
    which is the anchoring this design exists to avoid.
    """
    base = {"clip_id": c["clip_id"], "title": c.get("title"),
            "slot_type": c.get("slot_type")}
    if axis == "hook":
        lyr = c.get("lyrics") or ""
        return {**base, "theme": c.get("theme"), "song_form": c.get("song_form"),
                "hook_note": c.get("hook_note"), "lyric_opening": lyr[:600],
                "duration_s": c.get("duration_s")}
    if axis == "mix":
        return {**base, "qc": c.get("qc"), "duration_s": c.get("duration_s"),
                "requested_duration_s": c.get("requested_duration_s"),
                "style_string": c.get("style_string")}
    return {**base, "theme": c.get("theme"), "style_string": c.get("style_string"),
            "diversity_vector": c.get("diversity_vector")}


def _slot_ask(full_slots: int, short_slots: int) -> str:
    """Ask for the lanes that have slots. _honour_slots would discard a pick in a
    lane with none, but the ask is what stops it being made."""
    parts = [f"{n} {name} slots"
             for name, n in (("FULL", full_slots), ("SHORT", short_slots)) if n]
    return f"Fill {' and '.join(parts)}."


def _select(candidates: list[dict], signals: dict, *, full_slots: int,
            short_slots: int) -> dict:
    """Ask the model to choose, then enforce the slot contract deterministically."""
    by_type: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_type[c.get("slot_type", "full")].append(c)
    for group in by_type.values():
        group.sort(key=lambda c: c["score_total"], reverse=True)

    summary = [{
        "clip_id": c["clip_id"], "title": c.get("title"), "slot_type": c.get("slot_type"),
        "theme": c.get("theme"), "scores": {"hook": c["score_hook"], "mix": c["score_mix"],
                                            "trend": c["score_trend"], "total": c["score_total"]},
        "diversity_vector": c.get("diversity_vector"),
    } for c in candidates]

    try:
        result = ask_json(
            "producer", FINAL_SYSTEM,
            _slot_ask(full_slots, short_slots) + "\n\n"
            f"Candidates with scores:\n{json.dumps(summary, ensure_ascii=False)}\n\n"
            f"Today's themes:\n{json.dumps(signals.get('themes', [])[:8], ensure_ascii=False)}",
            schema_hint=FINAL_SCHEMA.replace(
                "SLOT_TYPES", "full|short" if short_slots else "full"),
            max_tokens=3000, temperature=0.5,
            label="selection")
    except ProviderError as exc:
        log.warning("producer selection failed (%s) — falling back to score order", exc)
        result = {}

    picks = _honour_slots(result.get("picks") or [], by_type,
                          full_slots=full_slots, short_slots=short_slots)
    picked_ids = {p["clip_id"] for p in picks}

    model_reasons = {int(r["clip_id"]): str(r.get("reason", ""))[:300]
                     for r in (result.get("rejections") or [])
                     if str(r.get("clip_id", "")).isdigit()}
    rejections = [{
        "clip_id": c["clip_id"],
        "reason": model_reasons.get(c["clip_id"])
                  or f"scored {c['score_total']:.2f}; slot went to a higher-ranked candidate",
    } for c in candidates if c["clip_id"] not in picked_ids]

    return {"picks": picks, "rejections": rejections,
            "rationale": str(result.get("rationale", ""))[:800]}


def _honour_slots(model_picks: list[dict], by_type: dict[str, list[dict]], *,
                  full_slots: int, short_slots: int) -> list[dict]:
    """The slot contract is not the model's to renegotiate.

    Take the model's choices where they are valid, then fill any shortfall from
    score order. A model that returns six full picks for five slots gets the
    top five, not an extra release.
    """
    wanted = {"full": full_slots, "short": short_slots}
    chosen: dict[str, list[int]] = {"full": [], "short": []}
    valid_ids = {st: {c["clip_id"] for c in group} for st, group in by_type.items()}

    for p in model_picks:
        try:
            cid = int(p["clip_id"])
        except (KeyError, TypeError, ValueError):
            continue
        st = p.get("slot_type")
        if st not in wanted:
            st = next((k for k, ids in valid_ids.items() if cid in ids), None)
        if st is None or cid not in valid_ids.get(st, set()):
            continue
        if cid in chosen[st] or len(chosen[st]) >= wanted[st]:
            continue
        chosen[st].append(cid)

    for st, need in wanted.items():
        for c in by_type.get(st, []):
            if len(chosen[st]) >= need:
                break
            if c["clip_id"] not in chosen[st]:
                chosen[st].append(c["clip_id"])

    reasons = {int(p["clip_id"]): str(p.get("why", ""))[:300]
               for p in model_picks if str(p.get("clip_id", "")).isdigit()}

    picks = []
    for st in ("full", "short"):
        for rank, cid in enumerate(chosen[st], start=1):
            picks.append({"clip_id": cid, "slot_type": st, "rank": rank,
                          "why": reasons.get(cid, "highest combined score in its slot type")})
    return picks
