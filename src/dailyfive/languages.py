"""The second language a song can carry, and the check that it will be legible.

One song is not translated. It is written in English and then ONE section — a
rap verse, a pre-chorus, a hook — is written in another language, the way a
feature verse works on a real record. That is the form that travels: a listener
who does not speak the language still gets the song, and a listener who does
gets a reason to send it to someone.

THE RENDERING CHECK IS THE POINT OF THIS MODULE. A lyric video burns text into
pixels, and a script with no font on the image comes out as a row of tofu boxes
in a delivered file — with ffmpeg exiting zero, the file decoding, and the
duration correct. Nothing downstream can notice. So a language is not a string
here, it is a claim about a script, and ``renderable()`` asks fontconfig whether
the claim holds on the machine that is about to render it.

That check found the bug it was written for: before the font packages went into
the Dockerfile, Korean was not renderable on the production image at all. DejaVu
Sans arrived there as a Chromium dependency and covers Latin and Arabic; it has
no Hangul. Every Korean lyric would have been boxes.

That check is why the roster is short and why it is not a config value. Adding
Hindi means adding a Devanagari font to the Dockerfile in the same commit, and
``test_every_offered_language_can_actually_be_rendered`` fails until it is
there.

Two things this module deliberately does not do.

It does not transliterate. The lyric goes to the generator in its own script,
because that is what the lyric video has to show and a romanised Hangul lyric
would render Latin letters over Korean singing.

It does not translate the whole song. A fully non-English song is a different
product with a different audience, and the studio has no way to judge whether
one is any good — the Producer's hook axis reads a lyric it can only assess in
English, and shipping songs nobody in the loop can evaluate is how a catalogue
fills up with material that scores well and is bad.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Language:
    code: str            # BCP-47-ish, the stored value; never renamed once shipped
    name: str            # what the Lyricist is told to write in
    script: str          # what has to have a font
    sample: str          # characters the render check actually looks for
    rtl: bool = False
    note: str = ""       # guidance the Lyricist needs and could get wrong


# Chosen for reach and for renderability, in that order, and every one of them
# has a font in the Dockerfile. Latin-script languages are close to free — the
# font is already there for English — so the cost of the roster is Korean and
# Arabic, which is one font package each.
LANGUAGES: tuple[Language, ...] = (
    Language("es", "Spanish", "Latin", "ñáéíóú¿¡",
             note="Latin American Spanish rather than peninsular — it is the "
                  "larger streaming audience and the one the charts reflect"),
    Language("fr", "French", "Latin", "éèêàçù",
             note="a spoken-register verse, not a chanson pastiche"),
    Language("pt", "Portuguese", "Latin", "ãõáêç",
             note="Brazilian Portuguese; the accent and the slang are not "
                  "interchangeable with European Portuguese"),
    Language("ko", "Korean", "Hangul", "네가없는밤이제일길어",
             note="Hangul, not romanised. A rap verse suits Korean phonology "
                  "better than a sung hook does"),
    Language("ar", "Arabic", "Arabic", "ماز لتأنتظر", rtl=True,
             note="Modern Standard Arabic or Levantine; write it right-to-left "
                  "in Arabic script and let the renderer handle direction"),
)

# Japanese was in this list and was taken out, and the reason is the one
# limitation of the check below worth knowing about.
#
# renderable() tests a SAMPLE, so it is only as good as the sample is
# representative — which holds for a closed script and fails for an open one.
# Hangul is 11,172 syllables from 24 letters and a Korean font has all of them;
# Arabic is 28 letters and their forms. Six words prove the script. Japanese
# kanji has no such bound: measured against the fonts this image installs, the
# sample "君のいない夜が" is covered by two fonts and the ordinary line
# "覚悟 憧憬 曖昧 躊躇 溢れる" is covered by none. The check would have said yes
# and production would have shipped tofu on the first lyric with normal kanji in
# it.
#
# Adding Japanese back means fonts-noto-cjk (about 60 MB) AND a sample that is
# not a proxy — most likely a coverage test over a real kanji frequency list
# rather than one line. Not worth it for a language nobody asked for.

BY_CODE = {lang.code: lang for lang in LANGUAGES}

# Where the foreign section can sit. A song keeps its English spine; this is
# which part of it changes hands.
PLACEMENTS: tuple[str, ...] = ("rap verse", "pre-chorus", "second verse",
                               "bridge", "hook ad-libs")


def renderable(lang: Language) -> bool:
    """Whether this machine has a font that covers the language's script.

    Asks fontconfig rather than reading a font file, because that is the same
    question libass asks at render time and the same answer it will get. A
    missing ``fc-list`` is reported as not renderable: an environment that
    cannot answer the question is not one to take a chance in.
    """
    if not shutil.which("fc-list"):
        log.warning("no fontconfig on this machine — cannot verify %s renders",
                    lang.name)
        return False
    try:
        got = subprocess.run(["fc-list", f":charset={_charset(lang.sample)}"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("font check for %s failed: %s", lang.name, exc)
        return False
    return bool(got.stdout.strip())


def _charset(sample: str) -> str:
    """fontconfig wants space-separated hex code points."""
    return " ".join(f"{ord(c):x}" for c in sample if not c.isspace())


def available() -> tuple[Language, ...]:
    """The roster this machine can actually deliver.

    Filtered rather than assumed, so a container built without the font packages
    ships English-only songs instead of tofu. The filter is a safety net and not
    a plan: the Dockerfile is expected to cover the whole roster, and a test
    fails if it does not.
    """
    ok = tuple(lang for lang in LANGUAGES if renderable(lang))
    missing = [lang.name for lang in LANGUAGES if lang not in ok]
    if missing:
        log.error("no font for %s — those languages will not be briefed. "
                  "Install the font packages named in the Dockerfile.",
                  ", ".join(missing))
    return ok


def get(code: str | None) -> Language | None:
    return BY_CODE.get((code or "").strip().lower()) if code else None


def brief_note(lang: Language, placement: str) -> str:
    """What the Lyricist is told, in the words it needs to act on."""
    return (
        f"ONE SECTION of this song — the {placement} — is in {lang.name}, and "
        f"the rest is English.\n"
        f"Write that section in {lang.name}, in {lang.script} script, as a "
        f"native speaker would say it. {lang.note}.\n"
        f"Do not translate the English lines and do not repeat their meaning in "
        f"{lang.name}: this is a different voice saying something the English "
        f"sections do not, the way a feature verse works. A listener who speaks "
        f"only English must still follow the song; a listener who speaks "
        f"{lang.name} must get something extra.\n"
        f"Do not romanise it and do not gloss it in brackets. The lyric file and "
        f"the video both carry the script as written."
    )


def assign(n_briefs: int, *, floor: int, run_date: date,
           roster: tuple[Language, ...] | None = None
           ) -> dict[int, tuple[Language, str]]:
    """Which briefs carry a second language today, which one, and where.

    Allocated in code rather than asked for, the same way the genre slate and
    the persona balance are. "Put a Spanish verse in sometimes" is an
    instruction a model follows on the days it happens to and forgets on the
    days it does not, and the studio would learn nothing from a signal that
    appears at a rate nobody set.

    Rotated by date rather than randomised, so it is deterministic — a re-run of
    a day produces the same assignment, and the console can say why a song had a
    Korean verse without the answer being "a coin came up". The rotation walks
    the roster evenly, which matters because the point of recording the language
    is to find out which ones travel: a roster sampled unevenly answers that
    question about the sampling.

    Returns {brief index: (language, placement)}. An index absent from the map
    is an English song, which is most of them.
    """
    pool = roster if roster is not None else available()
    want = max(0, min(floor, n_briefs))
    if not pool or not want:
        return {}

    # The day's ordinal is the cursor. Consecutive days therefore start at
    # consecutive languages rather than repeating, and over a roster of five
    # with two a day every language comes round in under a week.
    start = run_date.toordinal()
    out: dict[int, tuple[Language, str]] = {}
    for i in range(want):
        lang = pool[(start + i) % len(pool)]
        # Placement advances on the day AND on which lap of the roster it is,
        # so a language does not always arrive in the same part of the song. The
        # language rotation alone has a period equal to the roster size, and any
        # placement derived only from the day would share it — Spanish would be
        # the rap verse every single time. The extra term lengthens the pairing
        # cycle to roster x placements.
        placement = PLACEMENTS[(start + i + start // len(pool)) % len(PLACEMENTS)]
        out[i] = (lang, placement)
    return out
