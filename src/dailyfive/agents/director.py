"""Music Director — turns themes into checkable musical specification.

The distinction this agent exists to enforce: a prompt made of adjectives
produces a generation nobody can evaluate. A spec with a tempo, a key, a form
and a hook position produces one the QC Engineer can measure and the Archivist
can attribute a score to.

It reads the codex — including what the Archivist has learned — and writes back
proposed edits rather than mutating it directly.
"""

from __future__ import annotations

import json
import logging

from .. import genres
from ..codex import Codex
from .base import ask_json

log = logging.getLogger(__name__)

SYSTEM_HEAD = """You are the Music Director of an automated studio. You convert themes \
into production specifications precise enough that a generation can be checked \
against them afterwards.

Every spec you write must be CHECKABLE. Not "moody" — 84 BPM, F minor, hook at \
0:07, sub-heavy, no snare until bar 9. Not "energetic" — 128 BPM, four-on-floor, \
riser into a drop at 0:48.

You encode CHARACTERISTICS, never NAMES. Describing production technique, era, \
instrumentation and arrangement is your job. Naming a living recording artist as \
a target is not — it gets the generation refused by the model's own content \
filter, and it is not defensible even when it works. If you catch yourself \
reaching for "sounds like X", write down what X actually does instead."""

BRIEFINGS = {
    "full": """FULL specs are 2.5-3.5 minute songs. Arrangement matters, dynamics \
matter, the second verse should not be the first verse again.""",
    "short": """SHORT specs are 30-60 second cuts built to loop. Hook inside the \
first two seconds. No intro. The loop seam must be inaudible, so the last bar \
has to lead back into the first.""",
}

SYSTEM_TAIL = """Respect the codex's learned observations where they exist. They \
come from real measured outcomes, and they beat your priors. Where the codex is \
empty, say so rather than inventing a track record."""

# Two blocks, because they answer to different conditions. The genre fields are
# always asked for — a spec written on a day with no slate still has to carry
# the labels the next sixty days of evidence get joined against — while the
# slate itself only exists once there is something to allocate.
GENRE_BRIEFING = """Every spec carries two genre labels: a FAMILY and a \
SPECIFIC within it. Copy both VERBATIM from the vocabulary supplied below. They \
are not description, they are keys: they get joined against outcome ratings for \
months, so "country-folk" and "country folk" are two genres with no history \
each, which is exactly why the studio has learned nothing about genre so far. \
Say whatever you like about the sound in style_string — that is the free text. \
These two fields are controlled terms.

The genre field buys you NOTHING on the rest of the spec. "Country" is not a \
tempo, not a key, not a form and not a palette, and a style_string that leans \
on the label instead of describing the record is the adjective problem wearing \
a new hat. Write the spec exactly as you would if the genre fields were not \
there — same BPM, same key, same form, same hook position — and then fill them \
in. A spec whose style_string is a genre name with decoration around it is a \
spec nobody can check afterwards, which is the one thing this role exists to \
prevent.

The vocabulary contains CROSSBREEDS — electronic-hip-hop, techno-r-and-b, \
electro-r-and-b, jersey-club, phonk, hyperpop, latin-house, country-trap, \
melodic-techno, drum-and-bass — and they are ordinary choices, not exotic ones. \
Reach for them when the theme wants a beat under it. This catalogue is made to \
be used in vertical video, and that is not built out of piano ballads: the day \
this instruction was written, three of five released tracks sat at or below 96 \
BPM with two of them on piano and fingerpicked guitar.

Picking a fusion changes nothing about the rule above. It is still two \
controlled terms and a style_string that describes an actual record: what plays \
the pulse, what plays the chords, where the drop or the hook lands. \
"electronic-hip-hop" is a key, not a description."""

GENRE_SLATE = """Today's genre slate is decided before you and arrives with the \
themes. It is a set of COUNTS, not an assignment: it says how many of today's \
specs are country and how many are alt-R&B, and shows the evidence behind each. \
Which THEME gets which GENRE is your call and nobody else's — you are the only \
role that sees the situation and the specification at the same time, and a \
pairing you cannot write as a checkable spec is worse than a pairing that is \
off-trend.

A slate entry showing n: 0 is an experiment, not a track record. Do not write \
it into codex_notes as though it were one, and do not let it steer a spec \
harder than a genre with real ratings behind it would.

If a slate genre cannot be paired with any remaining theme without producing a \
spec you would not defend, use a different label from the vocabulary for at \
most ONE spec and give your reason in genre_off_slate_reason. Do not substitute \
silently: an off-slate choice with a reason is evidence about the slate, an \
unexplained one is noise."""

SCHEMA = """{
  "specs": [
    {
      "theme": "the theme this spec serves, copied verbatim",
      "slot_type": "SLOT_TYPES",
      "bpm": 0,
      "key": "F minor",
      "song_form": "Intro(4) - Verse(16) - ...",
      "instrumentation": "concrete palette, <= 200 chars",
      "hook_note": "where the hook lands and what makes it stick, <= 160 chars",
      "vocal_gender": "m|f",
      "style_string": "comma-separated production descriptors for the generator, <= 900 chars, NO artist names",
      "mix_note": "<= 120 chars",
      "genre_family": "one family, copied verbatim from the vocabulary",
      "genre": "one specific from that family, copied verbatim from the vocabulary",
      "genre_off_slate_reason": "only when genre_family is not on today's slate, <= 160 chars"
    }
  ],
  "codex_notes": ["observations worth carrying into the codex, <= 3 items"]
}"""


def _system(lanes: list[str], *, slate: bool = False) -> str:
    """The prompt describes only the lanes the run has slots for.

    A model told at length how to write a short cut writes some, and a spec in a
    lane with no slots is a brief the day never gets: the discard at the end of
    run() costs a generation, not a stray sentence. Building the briefing from
    the lanes is what keeps that true in either configuration.
    """
    blocks = [SYSTEM_HEAD, *(BRIEFINGS[lane] for lane in lanes), GENRE_BRIEFING]
    if slate:
        blocks.append(GENRE_SLATE)
    blocks.append(SYSTEM_TAIL)
    return "\n\n".join(blocks)


def _ask(counts: list[tuple[str, int]]) -> str:
    """One lane reads as a plain instruction; two need the total spelled out."""
    phrases = [f"{n} {name.upper()} specs" for name, n in counts]
    if len(phrases) == 1:
        return f"Write {phrases[0]}."
    return f"Write {' and '.join(phrases)} — {sum(n for _, n in counts)} in total."


def _schema(lanes: list[str]) -> str:
    return SCHEMA.replace("SLOT_TYPES", "|".join(lanes))


def _slate_section(slate: list[dict] | None) -> str:
    """The slate, or the honest sentence saying there is not one yet.

    An absent section would leave the model to infer why it is being asked for
    a genre with no evidence attached, and the SYSTEM_TAIL instinct — say the
    codex is empty rather than invent a track record — only works if the
    emptiness is stated.
    """
    vocab = f"Genre vocabulary (family: specifics):\n{json.dumps(genres.VOCABULARY)}\n\n"
    if not slate:
        return (vocab +
                "No genre slate today — the vocabulary has been read but nothing "
                "has been briefed yet. Choose each spec's genre yourself from the "
                "vocabulary above, and fill both fields anyway: they are the "
                "labels the next sixty days of evidence get joined against.\n\n")
    total = sum(int(e.get("specs") or 0) for e in slate)
    return (vocab +
            f"Today's genre slate — {total} specs:\n"
            f"{json.dumps(slate, ensure_ascii=False)}\n\n")


def run(themes: list[dict], codex: Codex, *, full_n: int, short_n: int,
        calibration: dict | None = None,
        slate: list[dict] | None = None) -> list[dict]:
    """Produce full_n + short_n musical specs from the ranked themes."""
    counts = [(name, n) for name, n in (("full", full_n), ("short", short_n)) if n]
    lanes = [name for name, _ in counts]
    want = full_n + short_n
    usable = themes[:max(want, len(themes))]
    forms = codex.body.get("song_forms", {})

    user = (
        f"{_ask(counts)}\n\n"
        f"Today's ranked themes:\n{json.dumps(usable, ensure_ascii=False)}\n\n"
        f"Sonic calibration from the lagging feeds:\n"
        f"{json.dumps(calibration or {}, ensure_ascii=False)}\n\n"
        + _slate_section(slate)
        + f"Codex v{codex.version}:\n{codex.brief_context(slots=lanes)}\n\n"
        + "".join(f"Available song forms — {lane}: "
                  f"{json.dumps(forms.get(lane, []))}\n" for lane in lanes)
        + "\nSpread the specs across tempo bands and moods. Two specs that would "
          "produce the same song are a wasted slot."
    )

    result = ask_json("director", _system(lanes, slate=bool(slate)), user,
                      schema_hint=_schema(lanes),
                      max_tokens=6000, temperature=1.0, label="specs")
    specs = result.get("specs") or []

    out: list[dict] = []
    for spec in specs:
        st = spec.get("slot_type")
        if st not in lanes:
            continue
        style = str(spec.get("style_string") or "").strip()
        if not style:
            continue
        # Same idiom as vocal_gender below: a value off the controlled set
        # becomes None here rather than being trusted downstream. A spec whose
        # genre does not normalise is kept and written with NULLs — dropping it
        # would cost a whole generation over a label, and the count of specs
        # that arrive off-vocabulary is the evidence that a term is missing.
        family, label = genres.normalise(spec.get("genre_family"), spec.get("genre"))
        cleaned = {
            "theme": str(spec.get("theme") or "")[:300],
            "slot_type": st,
            "bpm": _int_or_none(spec.get("bpm")),
            "key": str(spec.get("key") or "")[:40] or None,
            "song_form": str(spec.get("song_form") or "")[:400] or None,
            "instrumentation": str(spec.get("instrumentation") or "")[:300] or None,
            "hook_note": str(spec.get("hook_note") or "")[:240],
            "vocal_gender": spec.get("vocal_gender") if spec.get("vocal_gender") in ("m", "f") else None,
            "style_string": style[:900],
            "mix_note": str(spec.get("mix_note") or "")[:200],
            "genre_family": family,
            "genre": label,
            "genre_off_slate_reason": str(spec.get("genre_off_slate_reason") or "")[:160] or None,
        }
        if family is None:
            # The word the model chose, kept beside the null that replaced it.
            # Without this the count of off-vocabulary answers survives and the
            # evidence does not: genres.enforce() runs after this loop, reads a
            # field this loop has already nulled, and would log an empty string
            # where "afrobeats" belongs — and the missing term is the entire
            # thing the count is supposed to be pointing at.
            cleaned["genre_off_vocabulary"] = str(
                spec.get("genre_family") or spec.get("genre") or "")[:60] or None
        if family and _answered_with_the_label(cleaned):
            # The instruction not to lean on the label is a request; this is
            # what notices when it was not honoured. Recorded, never repaired:
            # rewriting a style_string here would put words in the Director's
            # mouth, and the count is what makes the failure arguable at the
            # end of the week instead of invisible.
            cleaned["genre_label_only"] = True
            log.warning("director: %r spec leans on the genre label rather than "
                        "specifying: %r", family, cleaned["style_string"][:120])
        out.append(cleaned)

    full = [s for s in out if s["slot_type"] == "full"][:full_n]
    short = [s for s in out if s["slot_type"] == "short"][:short_n]

    got = {"full": len(full), "short": len(short)}
    wanted = {"full": full_n, "short": short_n}
    if any(got[lane] < wanted[lane] for lane in lanes):
        log.warning("director returned %s, wanted %s",
                    ", ".join(f"{got[lane]} {lane}" for lane in lanes),
                    ", ".join(f"{wanted[lane]} {lane}" for lane in lanes))

    notes = [str(n)[:200] for n in (result.get("codex_notes") or [])][:3]
    for spec in full + short:
        spec["_codex_notes"] = notes
    return full + short


def _answered_with_the_label(spec: dict) -> bool:
    """Whether a spec answered with the genre word instead of the specification.

    Handing a model the word "country" invites it to write "country" back, and
    a style_string that is a genre name with a couple of adjectives around it
    is precisely the failure SYSTEM_HEAD exists to prevent — reappearing under
    a field that did not exist when SYSTEM_HEAD was written.

    Two conditions, and both are deliberately the degenerate case rather than a
    quality judgement. A style_string with nothing in it once the vocabulary
    terms are removed is a label, not a description. A spec carrying a genre
    but neither a tempo nor a form has had its checkable core crowded out by
    the label — those two fields are the ones SYSTEM_HEAD names, and they are
    separate keys precisely so the genre cannot swallow them.

    What this cannot catch is the middle: "country, upbeat, catchy" clears both
    tests and is still adjectives. Nothing here can measure that, and claiming
    otherwise would be worse than the gap.
    """
    fragments = [f.strip() for f in (spec.get("style_string") or "").split(",")]
    described = [f for f in fragments if f and genres.family_of(f) is None]
    if not described:
        return True
    return not spec.get("bpm") and not spec.get("song_form")


def _int_or_none(v) -> int | None:
    try:
        n = int(v)
        return n if 40 <= n <= 220 else None
    except (TypeError, ValueError):
        return None
