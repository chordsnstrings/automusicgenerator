"""Lyricist — two drafts per brief, then a forced choice.

Brain-agnostic like every other role: set ``LLM_LYRICIST`` to move it. MiniMax
is the natural default here because this is the highest-volume creative call in
the run and the cheapest capable model wins. ModelArk, the original suggestion,
exposes no text models at all — it is image, video and 3D only — so it earns
its place doing cover art instead.

Two drafts and a forced choice is not decoration: a single draft from any model
lands on the same handful of stock images (neon, rain, broken hearts), and
making the model choose between two of its own attempts reliably breaks that.
The effect is larger, not smaller, on cheaper brains — which is exactly why
this shape survives the move off a frontier model.
"""

from __future__ import annotations

import hashlib
import logging
import re

from ..errors import ProviderError
from .base import ask

log = logging.getLogger(__name__)

SECTION_TAGS = ["Intro", "Verse", "Pre-Chorus", "Chorus", "Post-Chorus",
                "Bridge", "Hook", "Outro"]

BANNED_IMAGERY = [
    "neon lights", "broken heart", "dancing in the rain", "shattered glass",
    "burning bridges", "paper planes", "city lights", "falling stars",
    "wildest dreams", "chasing shadows",
]

SYSTEM = """You write lyrics for released records. Not poetry, not a description \
of a song — the actual words a vocalist sings.

Rules that matter:
- Use section tags in square brackets: [Intro] [Verse] [Pre-Chorus] [Chorus] \
[Post-Chorus] [Bridge] [Hook] [Outro]. Match the song form you are given, \
including the bar counts — a 16-bar verse is roughly 8 sung lines.
- Concrete over abstract. A named object, a time of day, a specific room. \
"Your keys still on the hook by the door" beats "memories of you".
- The chorus must be singable and repeatable. It is the line someone hums a week \
later, so it carries the hook, not a new idea every time.
- Second verse advances the situation. It is not the first verse rephrased.
- No stock imagery. Specifically avoid: neon lights, broken hearts, dancing in \
the rain, shattered glass, burning bridges, city lights, falling stars, chasing \
shadows. If one of these is the first thing you reach for, reach again.
- Never quote or paraphrase an existing song's lyrics. Not a line, not a title.
- Do not name real people, brands or places you would need permission to use.

For a SHORT cut, the hook comes first and the whole thing is under 20 lines. It \
has to loop, so the last line should sit naturally before the first."""


def run(brief: dict, *, client=None) -> dict:
    """Write two drafts, pick one. Returns lyrics + hash + which draft won."""
    slot = brief.get("slot_type", "full")
    title = brief.get("title", "?")

    prompt = _brief_prompt(brief)
    drafts: list[str] = []
    for n in (1, 2):
        try:
            text = ask("lyricist", SYSTEM, prompt + _variation(n, slot),
                       max_tokens=2500, temperature=0.95 if n == 1 else 1.05,
                       label=f"draft{n}:{title[:24]}")
            cleaned = _clean(text)
            if cleaned:
                drafts.append(cleaned)
        except ProviderError as exc:
            log.warning("lyric draft %d failed: %s", n, exc)

    if not drafts:
        raise ProviderError("llm", f"no usable lyric draft for {title!r}",
                            retryable=True)

    if len(drafts) == 1:
        chosen, which = drafts[0], 1
    else:
        chosen, which = _choose(brief, drafts)

    warnings = _lint(chosen, slot)
    if warnings:
        log.info("lyric lint on %r: %s", brief.get("title"), "; ".join(warnings))

    return {
        "lyrics": chosen,
        "lyric_hash": hashlib.sha256(chosen.encode()).hexdigest()[:32],
        "draft_chosen": which,
        "draft_count": len(drafts),
        "lint": warnings,
    }


def _brief_prompt(brief: dict) -> str:
    bits = [
        f"Title: {brief.get('title')}",
        f"Slot: {brief.get('slot_type')}",
        f"About: {brief.get('theme')}",
    ]
    if brief.get("angle"):
        bits.append(f"Angle that makes it distinct: {brief['angle']}")
    if brief.get("song_form"):
        bits.append(f"Song form (follow exactly): {brief['song_form']}")
    if brief.get("hook_note"):
        bits.append(f"Hook: {brief['hook_note']}")
    if brief.get("bpm"):
        bits.append(f"Tempo: {brief['bpm']} BPM — phrase lines to sit on that")
    if brief.get("key"):
        bits.append(f"Key: {brief['key']}")
    dv = brief.get("diversity_vector") or {}
    if dv.get("person"):
        bits.append(f"Grammatical person: {dv['person']}")
    if brief.get("persona_name"):
        bits.append(f"Sung by: {brief['persona_name']}")
    return "\n".join(bits)


def _variation(n: int, slot: str) -> str:
    if n == 1:
        return "\n\nWrite the lyric. Return only the lyric with its section tags."
    length = "under 20 lines" if slot == "short" else "full length"
    return ("\n\nWrite a DIFFERENT lyric for the same brief — a different entry point, "
            f"a different central image, a different opening line. {length.capitalize()}. "
            "Return only the lyric with its section tags.")


def _choose(brief: dict, drafts: list[str]) -> tuple[str, int]:
    """Force a choice between the two drafts, defaulting to the first on doubt."""
    system = ("You are an A&R picking between two lyric drafts for the same song. "
              "Answer with exactly one character: 1 or 2. Nothing else.")
    user = (f"Brief: {brief.get('title')} — {brief.get('theme')}\n\n"
            f"DRAFT 1:\n{drafts[0]}\n\n"
            f"DRAFT 2:\n{drafts[1]}\n\n"
            "Which is more specific, more singable, and less reliant on stock imagery?")
    try:
        answer = ask("lyricist", system, user, max_tokens=8, temperature=0.0,
                     label=f"pick:{brief.get('title', '?')[:24]}")
    except ProviderError as exc:
        log.warning("lyric selection failed, keeping draft 1: %s", exc)
        return drafts[0], 1
    return (drafts[1], 2) if "2" in answer[:4] else (drafts[0], 1)


def _clean(text: str) -> str:
    """Strip commentary the model wrapped around the lyric."""
    text = re.sub(r"```[a-z]*\n?", "", text).strip()
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("["):
            start = i
            break
    body = "\n".join(lines[start:]).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body


def _lint(lyrics: str, slot: str) -> list[str]:
    """Report, never rewrite. The Clearance agent decides what blocks a run."""
    out = []
    low = lyrics.lower()
    for phrase in BANNED_IMAGERY:
        if phrase in low:
            out.append(f"stock imagery: {phrase!r}")
    if not re.search(r"\[(chorus|hook)\]", low):
        out.append("no [Chorus] or [Hook] section tag")
    tags = set(re.findall(r"\[([A-Za-z -]+)\]", lyrics))
    unknown = {t for t in tags if t.strip().title() not in
               {s.title() for s in SECTION_TAGS}}
    if unknown:
        out.append(f"unrecognised section tags: {sorted(unknown)}")
    line_count = len([ln for ln in lyrics.splitlines() if ln.strip() and not ln.strip().startswith("[")])
    if slot == "short" and line_count > 24:
        out.append(f"short cut has {line_count} sung lines, expected <= 20")
    if len(lyrics) > 4800:
        out.append(f"lyric is {len(lyrics)} chars, near the 5000 model cap")
    return out
