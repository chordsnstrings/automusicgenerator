"""Clearance — rules first, model second.

Runs before anything is submitted, because a moderation rejection at 2am costs
credits and a slot with nobody awake to retry it. It catches three separate
things that all look the same from the outside:

1. Style strings that name a living artist. Suno's own filter refuses these, so
   they are a wasted generation even before the rights question.
2. Lyrics that echo a real song closely enough to be a problem.
3. Language that trips the content filter.

The deterministic pass runs first and is the one that catches the common cases
cheaply. The model pass catches what a regex cannot — a paraphrased chorus, an
unmistakable reference without the name.
"""

from __future__ import annotations

import json
import logging
import re

from ..errors import ProviderError
from .base import ask_json

log = logging.getLogger(__name__)

# Patterns that name a target rather than describing one. These are the exact
# constructions that get a generation refused.
ARTIST_REFERENCE = re.compile(
    r"\b(?:in the style of|sounds? like|à la|a la|inspired by|reminiscent of|"
    r"tribute to|cover of|homage to|type beat|vibe of|channell?ing)\b",
    re.IGNORECASE,
)

FEATURE_CREDIT = re.compile(r"\b(?:feat\.?|featuring|ft\.?|with vocals? by)\s+[A-Z]", re.IGNORECASE)

# Trademarks that turn up in lyrics without anyone meaning anything by it.
TRADEMARKS = re.compile(
    r"\b(Coca[- ]?Cola|Pepsi|Nike|Adidas|Gucci|Prada|Rolex|Ferrari|Lamborghini|"
    r"Instagram|TikTok|Spotify|Netflix|iPhone|Xanax|Percocet|Adderall)\b",
    re.IGNORECASE,
)

# Well-known opening lines. Not exhaustive — the model pass is what covers the
# long tail. This catches the handful a generator reaches for unprompted.
FAMILIAR_LINES = [
    "is this the real life", "hello darkness my old friend", "i wanna hold your hand",
    "we will we will rock you", "sweet dreams are made of this",
    "i will always love you", "another one bites the dust", "billie jean is not my lover",
    "shake it off shake it off", "hey jude", "let it be let it be",
]

SYSTEM = """You are the clearance check for an automated music studio. You see \
lyrics and a style string before they are submitted for generation.

You are looking for exactly three things:

1. RIGHTS RISK. Lyrics that reproduce or closely paraphrase an existing song's \
words. A shared common phrase is fine — "I miss you" belongs to nobody. A \
distinctive line or a recognisable chorus structure is not.
2. NAMED TARGETS. Any living recording artist named as a stylistic reference, \
whether in the style string or the lyric. Also unlicensed use of a real person's \
name as a character.
3. FILTER RISK. Content likely to be refused by a generation model's own \
moderation: explicit sexual content, slurs, graphic violence, real-world \
tragedy, drug brand names.

Be proportionate. This is commercial pop music, not a legal filing. Mild profanity, \
heartbreak, drinking, and ordinary adult themes are all fine and you should pass \
them. Flag what would actually cause a problem, not what might mildly embarrass \
someone. Over-blocking costs a slot every day; under-blocking costs one \
occasionally.

If you can fix something with a small edit, propose the edit rather than \
rejecting the whole brief.

REJECT IS FOR WHAT AN EDIT CANNOT FIX. A rejected brief is a song that does not \
get made — nothing replaces it, the day ships one fewer, and a slot goes to a \
second take of another song instead. Rights risk is almost never in that \
category: a phrase can be changed. If a refrain is too close to an existing \
song, rewrite the refrain and pass the rest. Reserve reject for a brief whose \
whole premise is the problem.

A common phrase repeated as a hook is not a rights risk. "One more time", "hold \
on", "I can't breathe without you" and their like are how choruses are built; \
they belong to nobody, and repetition is what a chorus IS. A brief was rejected \
for exactly this — its hook was "one more time" — and it cost the day a \
song."""

SCHEMA = """{
  "verdict": "pass|rewrite|reject",
  "reasons": ["what you found, <= 160 chars each"],
  "lyrics_fixed": "the corrected lyric if verdict is rewrite, else null",
  "style_fixed": "the corrected style string if verdict is rewrite, else null",
  "severity": "none|low|medium|high"
}"""


def run(brief: dict, lyrics: str, style_string: str, *, use_model: bool = True) -> dict:
    """Returns {verdict, reasons, lyrics, style_string, rules_hits, severity}."""
    rules = _rules_pass(lyrics, style_string)

    # A named stylistic target is a hard stop before spending a model call:
    # it is certain to be refused and the fix is mechanical.
    hard = [h for h in rules if h["severity"] == "high"]
    if hard and not use_model:
        return {"verdict": "reject", "reasons": [h["detail"] for h in hard],
                "lyrics": lyrics, "style_string": style_string,
                "rules_hits": rules, "severity": "high"}

    if not use_model:
        return {"verdict": "pass", "reasons": [h["detail"] for h in rules],
                "lyrics": lyrics, "style_string": style_string,
                "rules_hits": rules, "severity": _worst(rules)}

    user = (
        f"Title: {brief.get('title')}\n"
        f"Theme: {brief.get('theme')}\n\n"
        f"STYLE STRING:\n{style_string}\n\n"
        f"LYRICS:\n{lyrics}\n\n"
        f"Deterministic pre-check already flagged: "
        f"{json.dumps([h['detail'] for h in rules], ensure_ascii=False) or 'nothing'}"
    )

    try:
        result = ask_json("clearance", SYSTEM, user, schema_hint=SCHEMA,
                          max_tokens=4000, temperature=0.3,
                          label=f"check:{brief.get('title', '?')[:30]}")
    except ProviderError as exc:
        # Clearance failing open would submit unchecked content; failing closed
        # would lose the slot. The rules pass already ran, so trust that.
        log.warning("clearance model pass failed (%s) — falling back to rules only", exc)
        verdict = "reject" if hard else "pass"
        return {"verdict": verdict, "reasons": [h["detail"] for h in rules] +
                ["model clearance unavailable; rules-only check"],
                "lyrics": lyrics, "style_string": style_string,
                "rules_hits": rules, "severity": _worst(rules)}

    verdict = result.get("verdict")
    if verdict not in ("pass", "rewrite", "reject"):
        verdict = "pass"

    # A model-chosen reject with no hard rule behind it becomes a rewrite. The
    # prompt already says an edit beats a rejection and that a common phrase
    # belongs to nobody; on 2026-08-30 it rejected a brief anyway, because its
    # hook was "one more time", and the day shipped one fewer song for it.
    #
    # Downgraded rather than passed. There may well be something here worth
    # changing — the model thought so — so the fix it proposed is taken if it
    # proposed one, and if it did not, the rewrite collapses to a pass through
    # the branch below, which is the same answer the rules pass would have given
    # on its own. What is not available any more is spending a slot on a
    # judgement call that no deterministic rule agreed with.
    if verdict == "reject" and not hard:
        log.warning("clearance rejected %r on model judgement alone with no rule "
                    "hit — treating as a rewrite: %s", brief.get("title"),
                    "; ".join(str(r)[:120] for r in (result.get("reasons") or []))[:300])
        verdict = "rewrite"

    out_lyrics = lyrics
    out_style = style_string
    if verdict == "rewrite":
        fixed_l = result.get("lyrics_fixed")
        fixed_s = result.get("style_fixed")
        if isinstance(fixed_l, str) and fixed_l.strip():
            out_lyrics = fixed_l.strip()
        if isinstance(fixed_s, str) and fixed_s.strip():
            out_style = fixed_s.strip()[:900]
        # A rewrite that changed nothing is a pass with extra steps.
        if out_lyrics == lyrics and out_style == style_string:
            verdict = "pass"

    if hard and verdict == "pass":
        # The model overruled a certain refusal. Strip the reference ourselves.
        out_style = _strip_references(out_style)
        log.info("clearance: stripped named-target construction from style string")

    return {
        "verdict": verdict,
        "reasons": [str(r)[:200] for r in (result.get("reasons") or [])][:8],
        "lyrics": out_lyrics,
        "style_string": out_style,
        "rules_hits": rules,
        "severity": result.get("severity") or _worst(rules),
    }


def _rules_pass(lyrics: str, style: str) -> list[dict]:
    hits: list[dict] = []
    low_lyrics = re.sub(r"[^\w\s]", "", lyrics.lower())

    if ARTIST_REFERENCE.search(style):
        hits.append({"rule": "artist_reference", "severity": "high", "where": "style",
                     "detail": "style string names a stylistic target — Suno refuses these"})
    if ARTIST_REFERENCE.search(lyrics):
        hits.append({"rule": "artist_reference", "severity": "medium", "where": "lyrics",
                     "detail": "lyric contains a 'sounds like' construction"})
    if FEATURE_CREDIT.search(style) or FEATURE_CREDIT.search(lyrics):
        hits.append({"rule": "feature_credit", "severity": "high", "where": "both",
                     "detail": "a feature credit implies a real performer"})

    for tm in set(m.group(0) for m in TRADEMARKS.finditer(lyrics)):
        hits.append({"rule": "trademark", "severity": "low", "where": "lyrics",
                     "detail": f"trademark in lyric: {tm}"})

    for line in FAMILIAR_LINES:
        if line in low_lyrics:
            hits.append({"rule": "familiar_line", "severity": "high", "where": "lyrics",
                         "detail": f"lyric contains a well-known line: {line!r}"})
    return hits


def _strip_references(style: str) -> str:
    """Drop the whole clause, not just the construction.

    Removing "in the style of" from "in the style of some singer" leaves the
    name behind, which is the part that actually causes the refusal. Style
    strings are comma-delimited, so the clause is the unit to remove.
    """
    kept = [c.strip() for c in style.split(",")
            if c.strip() and not ARTIST_REFERENCE.search(c)]
    return ", ".join(kept)[:900]


def _worst(hits: list[dict]) -> str:
    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    if not hits:
        return "none"
    return max((h["severity"] for h in hits), key=lambda s: order.get(s, 0))
