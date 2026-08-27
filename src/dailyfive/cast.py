"""The dancers, and the terms that are never negotiable.

A recurring cast rather than a stranger per song, for the same reason a label
signs artists rather than session singers: a channel is built out of
recognition, and a face that returns is worth more than a face that is new. Six
performers, rotated deterministically, each pinned to a stored reference still
so the same person appears for the length of a short rather than four people
who happen to be dressed alike.

Casting varies across the roster the way any music video's does. It is a cast
list, not a dial — nothing here reads a performance number and re-weights who
appears, because that would be optimising an audience's response to a person's
appearance and it is not a thing this studio does.

THE FIXED TERMS exist because two different failures share one fix.

Image models drift younger than the words they are given. "20 to 25" is a
request; without an explicit adult anchor repeated as its own clause, a
generated face lands wherever the training distribution is densest, and on
dance-adjacent prompts that is younger than asked. So the age language is
stated twice, positively and as a negative, and it is not built from a caller's
string.

And a short that reads as sexually suggestive is demonetised on both platforms
this ships to — YouTube's advertiser-friendly rules and TikTok's are explicit
about it. So the same clause that keeps the depiction adult also keeps it
clothed and non-suggestive, and it is appended after the caller's prompt rather
than before it, where a later instruction cannot be talked out of by an earlier
one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Appended to every generated frame and every clip, last, verbatim. Not
# formatted, not parameterised, not reachable from a brief or a model's output.
FIXED_TERMS = (
    "Adult woman in her early twenties, clearly of adult age, mature adult "
    "features. Fully clothed in ordinary streetwear. Dancing, non-sexualised, "
    "no suggestive posing, no revealing clothing, no close-up on body parts. "
    "Natural realistic proportions. No text, no watermark, no logos, no captions."
)

# Refused outright rather than filtered, because a negative prompt is a request
# and this is not. A brief that reaches for any of these does not get a video.
FORBIDDEN = (
    "teen", "teenage", "schoolgirl", "young girl", "little girl", "child",
    "loli", "jailbait", "barely legal", "underage", "minor",
    "nude", "naked", "topless", "lingerie", "bikini", "underwear",
    "seductive", "sensual", "erotic", "sexy", "provocative", "twerk",
)


@dataclass(frozen=True)
class Performer:
    key: str
    look: str          # what Seedream is asked for; no age term — FIXED_TERMS owns that
    energy: str        # how they move, so the Director can match a performer to a tempo


# Six is enough that a viewer sees variety within a week and few enough that
# each returns often enough to be recognised. Descriptions carry appearance,
# wardrobe and setting only; every one of them is dancing, adult and clothed
# because FIXED_TERMS says so for all of them at once.
CAST: tuple[Performer, ...] = (
    Performer("nia", "Black woman, natural coil afro, warm brown skin, gold hoop "
                     "earrings, oversized cream knit and wide denim, sunlit loft "
                     "with tall windows", "loose, grounded, shoulder-led"),
    Performer("mei", "East Asian woman, straight black bob, oversized grey blazer "
                     "over a white tee, black trousers, concrete stairwell with a "
                     "single hard light", "sharp, precise, stop-and-go"),
    Performer("sofia", "Latina woman, dark wavy hair to the shoulder, olive skin, "
                       "rust corduroy jacket and straight jeans, rooftop at golden "
                       "hour", "fluid, hip-led, continuous"),
    Performer("priya", "South Asian woman, long dark hair, deep brown skin, olive "
                       "utility jacket and cargo trousers, neon-lit underpass at "
                       "night", "quick footwork, low centre"),
    Performer("hanna", "White woman, shoulder-length auburn hair, freckles, black "
                       "turtleneck and pleated skirt over tights, empty rehearsal "
                       "room with mirrors", "long lines, arms-led, unhurried"),
    Performer("amara", "Mixed-race woman, tight curls pulled back, brown skin, "
                       "faded band tee and leather jacket, rain-wet street under "
                       "shopfront light", "punchy, bouncing, weight forward"),
)

CAST_BY_KEY = {p.key: p for p in CAST}


class UnsafeBrief(ValueError):
    """The brief asks for something no cast member will be prompted to do."""


def screen(text: str) -> None:
    """Refuse before spending, not after looking at the result.

    Raises rather than sanitising. A brief that reaches for a forbidden term is
    a brief whose theme is wrong, and quietly stripping the word would produce a
    video for it anyway.
    """
    low = f" {text.lower()} "
    hit = [w for w in FORBIDDEN if w in low]
    if hit:
        raise UnsafeBrief(f"brief contains {', '.join(sorted(hit))}")


def pick(*, run_date: str, clip_id: int) -> Performer:
    """Which performer today, deterministically.

    Hashed rather than round-robined on a counter: the studio ships one short a
    day and a stored counter would need a column, a migration and a failure mode
    of its own. The same day and clip always resolve to the same performer, so a
    re-run after a crash re-casts the same person rather than a different one
    halfway through a short.
    """
    seed = hashlib.sha256(f"{run_date}:{clip_id}".encode()).digest()
    return CAST[seed[0] % len(CAST)]


def still_prompt(p: Performer) -> str:
    """The Seedream reference still. One per short; every clip animates from it."""
    return (
        f"Photorealistic full-body photograph. {p.look}. "
        "Standing, relaxed, facing camera, neutral expression, natural daylight "
        "or practical light, shallow depth of field, 35mm look, film grain. "
        f"{FIXED_TERMS}"
    )


def clip_prompt(p: Performer, shot: str, *, bpm: int | None = None) -> str:
    """One Seedance clip. `shot` is the Video Director's line for this beat.

    The tempo is stated because a generated dance has no idea what it is dancing
    to — Seedance never hears the track. It is a nudge toward the right energy,
    not synchronisation; synchronisation is the cutter's job and it is done with
    the beat grid, on the edit, where it can actually be made exact.
    """
    screen(shot)
    tempo = ""
    if bpm:
        feel = "slow and heavy" if bpm < 90 else "mid-tempo" if bpm < 120 else "fast and driving"
        tempo = f" Movement {feel}, around {bpm} beats per minute."
    return (
        f"Photorealistic video. {p.look}. "
        f"{shot.strip().rstrip('.')}. Movement is {p.energy}.{tempo} "
        "Handheld camera, natural motion, single continuous shot, no cuts. "
        f"{FIXED_TERMS}"
    )
