"""Video Director — what the camera sees for each cut of the short.

The failure this role exists to prevent is the one that was shipped and
rejected: two clips generated from the same request produce two near-identical
takes, and cutting between them reads as a loop. The viewer sees the trick at
about three seconds and leaves. Nothing in the pipeline noticed, because both
clips were individually fine.

So framing is a controlled field rather than prose. The model chooses it, but
the code enforces that consecutive shots differ, the same way the A&R's
diversity check enforces what its prompt merely asks for. A model told to write
two different shots will write two different sentences about the same shot; a
ladder that cannot repeat a framing produces a cut whether or not the model
cooperated.

Two things this role never asks for, both because no generator here can do them
and a request for something impossible comes back visibly broken:

*Lip-sync.* The performer is listening to the song, not performing it. A shot
description implying she sings the line produces a mouth moving out of time with
audio the generator never heard.

*Text.* No lyric on the wall, no phone screen with words on it, no title card.
The short carries no text at all — that was settled when the overlay came off —
and a generated sign says something no one wrote.

The output is a physical description: what her body does, where the camera is.
Nothing about the song's meaning, because a generator cannot render meaning and
will render the words instead.
"""

from __future__ import annotations

import logging

from ..cast import FORBIDDEN, Performer
from .base import ask_json

log = logging.getLogger(__name__)

# ── the controlled fields ────────────────────────────────────────────────────
# Spelled out rather than passed through, so the generator gets the same words
# every time and the model only has to pick. A free-text "wide shot" from a
# model arrives as "wide angle", "full body", "far away" on three different days
# and the three clips do not look like the same show.
FRAMINGS: dict[str, str] = {
    "wide": "Full-body wide shot, the whole mirror and the bedroom in frame",
    "mid": "Mid shot from the waist up, the mirror filling the frame behind her",
    "close": "Close shot on her head and shoulders, the edge of the mirror just in frame",
}

MOVES: dict[str, str] = {
    "locked": "Camera locked off at chest height, no movement",
    "push": "Camera pushes in slowly and steadily",
    "pull": "Camera pulls back slowly and steadily",
    "drift": "Handheld camera drifting slightly, small natural sway",
    "arc": "Camera arcs slowly around her",
}

# The order a cut works in when the model gives nothing usable: start wide so the
# room reads, come in as the section builds. Consecutive entries never share a
# framing, which is the property the whole file is built to guarantee.
LADDER: tuple[tuple[str, str], ...] = (
    ("wide", "locked"),
    ("mid", "push"),
    ("close", "drift"),
    ("wide", "arc"),
    ("mid", "pull"),
    ("close", "locked"),
)

# Physical, unremarkable, and safe to ship. Used when the model's action is
# unusable rather than when it is merely dull — a dull shot that a person wrote
# still beats a stock one.
FALLBACK_ACTIONS: tuple[str, ...] = (
    "she finds the beat, weight shifting from foot to foot, watching herself in the glass",
    "the routine opens up, arms leading, a turn on the last beat of the bar",
    "footwork quickens, shoulders driving, she catches her own eye in the mirror",
    "she steps back from the mirror, one full turn, then straight back into the count",
)

# A shot that asks for any of these comes back broken, so it is discarded and
# replaced rather than sent. Distinct from cast.FORBIDDEN, which is a safety
# rule and raises; this is a capability rule and repairs.
IMPOSSIBLE = (
    "sing", "sung", "lip", "mouth the", "mouthing", "vocal", "lyric",
    "text", "caption", "subtitle", "title card", "word", "writing", "written",
    "sign", "poster reads", "screen shows",
)


def _uses(text: str, terms) -> list[str]:
    low = f" {text.lower()} "
    return sorted({t for t in terms if t in low})


SYSTEM = """You are the Video Director of an automated music studio. You write \
the shot list for a vertical short: one performer dancing to the song in front of \
a full-length mirror in her bedroom, cut on the beat.

You are directing a camera, not writing about a song. Every line you write \
describes something a camera could photograph: where her weight is, what her arms \
do, which way she turns, what the camera does while she does it. If a line of \
yours could not be checked by looking at the footage, it is not a shot.

Three hard rules.

She is LISTENING to the track, never performing it. Nothing you write may imply \
she is singing, mouthing or speaking. The generator never hears the song, so a \
mouth moving to words produces footage that reads as broken.

NOTHING IN THE FRAME HAS WRITING ON IT. No lyric on the wall, no phone screen \
with words, no poster you can read, no title card. Generated text is always wrong \
and there is no text in this format at all.

CONSECUTIVE SHOTS MUST NOT SHARE A FRAMING. Two shots at the same distance cut \
together into a loop, which is the single thing that kills this format — a viewer \
sees the repeat at about three seconds and leaves. Change the distance, and give \
the second shot a reason to follow the first: something builds, opens up, drops \
back.

Match her energy, which is given to you, and the tempo. A performer described as \
sharp and stop-and-go does not get a shot asking for flowing continuous movement; \
you would be directing against the only person in the frame."""

SCHEMA = """{
  "shots": [
    {
      "framing": "wide|mid|close",
      "move": "locked|push|pull|drift|arc",
      "action": "what her body does, physical only, <= 200 chars",
      "why_it_follows": "why this shot comes after the previous one, <= 120 chars"
    }
  ]
}"""


def plan(brief: dict, performer: Performer, *, shots: int = 2,
         seconds_each: float = 10.0, bpm: int | None = None) -> list[dict]:
    """Ask for a shot list; return one that is guaranteed usable.

    Guaranteed in the narrow sense that matters downstream: the right number of
    shots, every framing and move from the controlled sets, no two consecutive
    shots at the same distance, and no action that asks for something the
    generator cannot render. The model supplies the taste; this supplies the
    floor.
    """
    tempo = f"{bpm} BPM" if bpm else "tempo not specified"
    user = (
        f"Song: {brief.get('title') or 'untitled'}\n"
        f"What it is about: {(brief.get('theme') or '')[:300]}\n"
        f"Sound: {(brief.get('style_string') or '')[:300]}\n"
        f"Tempo: {tempo}\n"
        f"Where the hook lands: {(brief.get('hook_note') or 'unspecified')[:200]}\n\n"
        f"Your performer moves like this: {performer.energy}.\n"
        f"She looks like this, and you do not change any of it: {performer.look}\n\n"
        f"Write exactly {shots} shots, each {seconds_each:.0f} seconds long, in "
        f"the order they will be cut. Together they cover about "
        f"{shots * seconds_each:.0f} seconds of the song's hook."
    )

    try:
        result = ask_json("videodirector", SYSTEM, user, schema_hint=SCHEMA,
                          max_tokens=1500, temperature=1.0, label="shots")
        raw = result.get("shots") or []
    except Exception as exc:                      # the video still ships
        # Broad on purpose. Every other agent's failure stops the run because
        # its output is the run; this one's output is a nicety on top of a song
        # that already exists, and a day that ships no short because a shot list
        # would not parse is a worse day than one that ships the ladder.
        log.warning("video director unavailable, using the ladder: %s", exc)
        raw = []

    return _repair(raw, performer, shots=shots)


def _repair(raw: list, performer: Performer, *, shots: int) -> list[dict]:
    """Force the model's answer onto the controlled sets, or replace it."""
    out: list[dict] = []
    for i in range(shots):
        got = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
        framing = str(got.get("framing") or "").strip().lower()
        move = str(got.get("move") or "").strip().lower()
        action = str(got.get("action") or "").strip()

        if framing not in FRAMINGS:
            framing = LADDER[i % len(LADDER)][0]
        if move not in MOVES:
            move = LADDER[i % len(LADDER)][1]

        # The anti-loop rule, enforced rather than requested. Nudged one step
        # along the ladder rather than to a fixed alternative, so a model that
        # repeats itself does not collide into the same repair every time.
        if out and framing == out[-1]["framing"]:
            log.info("video director repeated %r framing on shot %d; stepped it",
                     framing, i)
            options = [f for f in FRAMINGS if f != out[-1]["framing"]]
            framing = options[i % len(options)]

        bad = _uses(action, IMPOSSIBLE) if action else ["empty"]
        unsafe = _uses(action, FORBIDDEN) if action else []
        if bad or unsafe:
            if unsafe:
                # Not repaired quietly: a shot list that reaches for one of
                # these is worth seeing in the logs even though the video ships.
                log.warning("video director shot %d asked for %s — replaced",
                            i, ", ".join(unsafe))
            elif action:
                log.info("video director shot %d asked for %s the generator "
                         "cannot render — replaced", i, ", ".join(bad))
            action = FALLBACK_ACTIONS[i % len(FALLBACK_ACTIONS)]

        out.append({
            "framing": framing,
            "move": move,
            "action": action[:220],
            "why_it_follows": str(got.get("why_it_follows") or "")[:160],
        })
    return out


def shot_line(shot: dict) -> str:
    """One shot as the single sentence :func:`dailyfive.cast.clip_prompt` wants.

    Framing first, then the camera, then her — which is the order a generator
    weights it. The performer's own description and the fixed terms are added by
    ``clip_prompt``; nothing here restates them, because two descriptions of the
    same woman in one prompt is how you get two different women in one short.
    """
    return (f"{FRAMINGS[shot['framing']]}. {MOVES[shot['move']]}. "
            f"She {shot['action'].lstrip().removeprefix('she ').lstrip()}")
