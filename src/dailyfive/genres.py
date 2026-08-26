"""The genre vocabulary, and the Genre Director that allocates it.

Six months of style strings taught the studio nothing about genre, because
every style string is unique prose. ``archivist._style_tokens`` splits
"country-soul hybrid at the uptempo edge of midtempo" on commas and gets
fragments no second song will ever repeat, so no genre-bearing token ever
reaches ``MIN_OBSERVATIONS`` and the learned table fills up with whatever
short stub happened to recur. Two controlled columns fix that: a label that
repeats by construction is a label that can accumulate.

Two levels, because they answer different questions on different clocks.
Families answer "is country working" in about a fortnight; specifics answer
"is it country-trap or country-soul" in about a quarter, and that is the
question that changes a prompt. Both come off the same rows.

There is no language model in this module and there is not meant to be one.
Scoring is a GROUP BY with a minimum sample count; allocation is UCB1 with a
cap. A model asked to allocate returns a different slate each morning from
identical numbers, and the ability to attribute a change in output to a change
in evidence is the entire point of keeping a versioned record at all. The one
part of the job that is genuine judgement — deciding that "country-soul hybrid
at the uptempo edge of midtempo" is ``country > country-soul`` — belongs to the
Music Director, who is already reading the specification when the decision is
made.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from .db import session_scope
from .models import Brief, Clip, Outcome, Run

log = logging.getLogger(__name__)


# ── the vocabulary ───────────────────────────────────────────────────────────
# The taxonomy is code, not codex and not config, and both halves of that are
# decisions a reader will want argued.
#
# Not config: config varies per deployment, and a taxonomy that varies per
# deployment makes a stored label uninterpretable without also knowing what an
# environment variable said on the day it was written — the opposite of a
# learning record.
#
# Not the codex, which is the tempting answer because the codex is where
# "what the studio knows" lives. Two reasons. The weekly retro already edits
# tempo_bands, avoid and instrumentation_palettes straight from model output,
# so a model that can add a tempo band could add a genre, and free text would
# creep back in one codex version at a time — the exact failure these columns
# exist to end. And the codex has already failed at this job once:
# ``archivist._bpm_bucket`` hardcodes ("ballad", (0, 78)), ("midtempo",
# (79, 104))… while ``SEED_CODEX["tempo_bands"]`` holds the same ranges and the
# retro is explicitly allowed to edit them. The day the retro moves the
# midtempo floor from 79 to 84, the Director briefs against one taxonomy and
# the aggregator buckets against another, silently and forever. A vocabulary
# that the briefer, the aggregator, the A&R duplicate check, the ID3 tagger and
# the meta.json writer all have to agree on cannot live somewhere only one of
# them reads. A deploy changes all six at once; a codex row cannot.
#
# THE ONE RULE FOR CHANGING THIS FILE: a label that has shipped is never
# renamed and never removed. Add freely — the columns are plain String, so a
# new label needs no migration. Renaming orphans every row that carries the old
# value and silently splits one track record into two halves, neither of which
# clears the sample threshold. Git is the history and the pull request is the
# rationale. ``test_shipped_labels_are_never_renamed`` enforces it.
#
# What the codex keeps is what has been *learned* — the scores under
# ``learned.genre_scores``, versioned and auditable. The label set is code.
# That division is the whole answer.

FAMILIES: tuple[str, ...] = (
    "pop", "country", "hip-hop", "r-and-b", "rock",
    "alternative", "electronic", "folk", "latin",
)

# Nine and not thirty-seven at this level, and the arithmetic is the reason.
# Seven briefs a day ship five songs, so at most five rated briefs a day even
# at perfect coverage. A 60-day record therefore holds at most 300 rated
# briefs, which at GENRE_MIN_RATED = 8 is 37 labels at the ceiling — exactly
# the number of specifics, i.e. the specific level can never all rank at once
# and is not meant to. Nine families clears the same ceiling four times over
# and returns a first verdict inside a fortnight.
#
# Deliberately absent, and the absence is the decision rather than an
# oversight: christian, jazz, blues, reggae, metal, k-pop, j-pop, afrobeats,
# classical, world. The cast is three English-language personas with fixed
# sonic territories (Vale restrained alt-pop, Rook electronic soul, Marisol
# warm uptempo live-band). Every family in this tuple costs roughly two weeks
# of exploration budget to evaluate, and a family the studio cannot credibly
# make burns that budget to learn something already known. Chart entries in an
# excluded genre are counted as outside the roster and reported as such, never
# forced into a family they do not belong to.

SPECIFICS: dict[str, str] = {
    "alt-pop": "pop",
    "synth-pop": "pop",
    "dance-pop": "pop",
    "bedroom-pop": "pop",

    "country-folk": "country",
    "country-pop": "country",
    "country-soul": "country",
    "country-trap": "country",
    "outlaw-country": "country",

    "trap": "hip-hop",
    "boom-bap": "hip-hop",
    "cloud-rap": "hip-hop",
    "drill": "hip-hop",

    "alt-r-and-b": "r-and-b",
    "neo-soul": "r-and-b",
    "contemporary-r-and-b": "r-and-b",
    "gospel-soul": "r-and-b",

    "indie-rock": "rock",
    "garage-rock": "rock",
    "heartland-rock": "rock",
    "post-punk": "rock",

    "dream-pop": "alternative",
    "shoegaze": "alternative",
    "art-rock": "alternative",
    "emo": "alternative",

    "house": "electronic",
    "ambient-electronic": "electronic",
    "synthwave": "electronic",
    "uk-garage": "electronic",

    "indie-folk": "folk",
    "singer-songwriter": "folk",
    "americana": "folk",
    "chamber-folk": "folk",

    "reggaeton": "latin",
    "latin-pop": "latin",
    "bachata": "latin",
    "cumbia": "latin",
}

# family -> its specifics, in declaration order. This is what goes into the
# Director's prompt, so the order is the order a reader sees.
VOCABULARY: dict[str, list[str]] = {
    family: [s for s, f in SPECIFICS.items() if f == family] for family in FAMILIES
}

# ID3v2 TCON. What a music player groups a library by, so it has to be a word
# players and DSPs recognise — which is why it is a fixed short name per family
# and not our internal slug. "r-and-b" is a database key; "R&B" is a genre tag.
ID3_NAME: dict[str, str] = {
    "pop": "Pop",
    "country": "Country",
    "hip-hop": "Hip-Hop",
    "r-and-b": "R&B",
    "rock": "Rock",
    "alternative": "Alternative",
    "electronic": "Electronic",
    "folk": "Folk",
    "latin": "Latin",
}

# Spellings that are the same term, not a different one. Kept deliberately
# short: this is a code decision about orthography ("r&b" and "r-and-b" are one
# genre), never an inference about what an unfamiliar label might mean. Anything
# not here and not in the vocabulary is off-vocabulary and gets recorded as
# such, which is the evidence that the vocabulary needs a new term.
ALIASES: dict[str, str] = {
    "r-and-b": "r-and-b",
    "r-b": "r-and-b",
    "rnb": "r-and-b",
    "randb": "r-and-b",
    "rhythm-and-blues": "r-and-b",
    "hip-hop-rap": "hip-hop",
    "hiphop": "hip-hop",
    "rap": "hip-hop",
    "alt-rnb": "alt-r-and-b",
    "alt-r-b": "alt-r-and-b",
    "alternative-r-and-b": "alt-r-and-b",
    "contemporary-rnb": "contemporary-r-and-b",
    "edm": "electronic",
    "dance": "electronic",
    "electronica": "electronic",
    "electro": "electronic",
    "singer-songwriter": "singer-songwriter",
    "americana": "americana",
}


# ── outside labels ───────────────────────────────────────────────────────────
# Keyed on the numeric id, never on the display name. Verified against the live
# Apple feed across fourteen regions today: genreId 7 arrives as Electronic,
# Elektronisch, Elettronica, Electrónica and Elektroniskt; genreId 18 as
# Hip-Hop/Rap, Hiphop/Rap, Hip-hop/Rap and ヒップホップ／ラップ; genreId 34 under
# seven different names. Keying on the name gives roughly thirty labels where
# there are nineteen genres, and splits one chart across five of them.
#
# Ids arrive as strings from Apple and as integers from Deezer; both maps are
# keyed by string and the lookup helpers coerce, so a dialect difference at the
# boundary cannot silently miss every row.
APPLE_UMBRELLA_IDS: frozenset[str] = frozenset({"34"})
# genreId 34 is on 50 of 50 entries in every region checked. It is the parent
# node of the whole music tree, not a genre, and counting it produces a chart
# in which the leading genre is "Music" by a factor of two.

APPLE_GENRE_IDS: dict[str, str] = {
    "6": "country",
    "7": "electronic",
    "10": "folk",            # Singer/Songwriter
    "12": "latin",
    "14": "pop",
    "15": "r-and-b",         # R&B/Soul
    "17": "electronic",      # Dance
    "18": "hip-hop",         # Hip-Hop/Rap
    "20": "alternative",
    "21": "rock",
}

# Recognised and deliberately not made. Kept as a map rather than left to fall
# through as unknown so the console can tell "the chart is 12% a genre we chose
# not to make" from "the chart contains an id we have never seen", which are
# different facts and only the second one is a reason to touch this file.
APPLE_OUTSIDE_ROSTER: dict[str, str] = {
    "2": "blues",
    "4": "children's music",
    "5": "classical",
    "8": "holiday",
    "11": "jazz",
    "13": "new age",
    "16": "soundtrack",
    "19": "world",
    "22": "christian",
    "23": "vocal",
    "24": "reggae",
    "25": "easy listening",
    "27": "j-pop",
    "51": "k-pop",
    "1153": "metal",
    "1185": "indian pop",
    "1263": "bollywood",
    "1264": "tamil",
    "1266": "regional indian",
    "1267": "devotional",
}

# Deezer's /genre endpoint, all 28 entries, fetched today. The ones that land
# in the roster:
DEEZER_GENRE_IDS: dict[str, str] = {
    "132": "pop",
    "116": "hip-hop",        # Rap/Hip Hop
    "122": "latin",          # Reggaeton
    "152": "rock",
    "113": "electronic",     # Dance
    "165": "r-and-b",        # R&B
    "85": "alternative",
    "106": "electronic",     # Electro
    "466": "folk",
    "84": "country",
    "67": "latin",           # Salsa
    "65": "latin",           # Traditional Mexicano
    "169": "r-and-b",        # Soul & Funk
    "71": "latin",           # Cumbia
    "197": "latin",          # Latin Music
}

DEEZER_OUTSIDE_ROSTER: dict[str, str] = {
    "0": "all",
    "186": "christian",
    "144": "reggae",
    "129": "jazz",
    "98": "classical",
    "173": "films/games",
    "464": "metal",
    "2": "african",
    "16": "asian",
    "153": "blues",
    # Brazilian is the row a reader will argue with, so it is argued here. The
    # industry often files it under Latin, but every specific in our latin
    # family is Spanish-language Caribbean or Mexican, and rolling sertanejo or
    # Brazilian funk into a family whose prompts are reggaeton and bachata
    # would make the family's track record mean two different things. Outside
    # the roster is the honest answer until there is a specific for it.
    "75": "brazilian",
    "81": "indian",
    "95": "kids",
}


# ── thresholds ───────────────────────────────────────────────────────────────
# These are learning-integrity rules and not deployment settings, which is why
# they live here and not in config.py: a deployment that lowers GENRE_MIN_RATED
# is a deployment whose numbers mean something different from everyone else's.
# GENRE_EXPLORE_BRIEFS is the exception and does live in config, because how
# much of a day to spend on exploration is an operational choice.

GENRE_MIN_RATED = 8
# Rated *briefs*, not clips, before a family's mean is shown as a number.
# Eight and not archivist.MIN_OBSERVATIONS = 4 for two reasons. Ratings run
# 1-10, a real difference between families is around a point, and a plausible
# per-song SD is 1.5-2 (honestly unknown), so SE = sd/sqrt(n) puts n=8 at about
# 0.53 — enough to be interesting, not enough to be sure, which is exactly what
# the THIN banner claims. And MIN_OBSERVATIONS governs whether a token appears
# in a prompt, while this governs reallocating the whole day's scarce and
# irreversible generation budget.

GENRE_WARM_FAMILIES = 3   # families at that bar before preference may steer the slate
GENRE_WARM_TOTAL = 30     # rated briefs overall before the same

GENRE_MAX_PER_FAMILY = 2
# Two of seven, enforced after the fact rather than requested in a prompt, in
# the register of anr._rebalance_personas: "No persona takes more than half the
# day. Enforced, not requested."

TASTE_HALF_LIFE_DAYS = 90.0
# Long, and deliberately so. At five rateable songs a day with one rater, a
# 30-day half-life discards half the evidence before most families have reached
# the threshold at all. The window has to be long relative to how fast evidence
# arrives, and here it arrives slowly.

UCB_C = 1.0
# Auer, Cesa-Bianchi & Fischer 2002, "Finite-time Analysis of the Multiarmed
# Bandit Problem", Machine Learning 47:235-256.

_UCB_WHY = "\x00ucb"   # placeholder, replaced in slate() with the printed sum


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── labelling ────────────────────────────────────────────────────────────────
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: object) -> str:
    """Lowercase, punctuation to single hyphens. "Country Folk" -> "country-folk".

    The Director is told to copy labels verbatim and mostly will, but
    "country-folk" and "country folk" landing as two labels with no history
    each is precisely how the studio has learned nothing about genre so far.
    Slugging costs nothing and removes the whole class.
    """
    if not isinstance(value, str):
        return ""
    return _SLUG_RE.sub("-", value.strip().lower()).strip("-")


def normalise(family: object, specific: object) -> tuple[str | None, str | None]:
    """Map a proposed (family, specific) pair onto the vocabulary.

    Returns ``(None, None)`` for anything off-vocabulary rather than guessing.
    A spec that comes back with no genre is written with NULLs and counted, not
    dropped: discarding it would cost a whole generation over a label, and the
    count of off-vocabulary answers is the evidence that a term is missing.

    The specific is authoritative when the two disagree. It is the more
    specific claim, its family is unambiguous from SPECIFICS, and a Director
    that writes "country-soul" under the family "r-and-b" has told us something
    real about the spec and something merely careless about the header.
    """
    fam = _slug(family)
    fam = ALIASES.get(fam, fam)
    spec = _slug(specific)
    spec = ALIASES.get(spec, spec)

    if spec in SPECIFICS:
        owner = SPECIFICS[spec]
        if fam and fam != owner:
            log.info("genre: %r is %s, not %r — taking the specific",
                     spec, owner, fam)
        return owner, spec
    if fam in FAMILIES:
        if spec:
            log.info("genre: %r is not a specific of any family — family only", spec)
        return fam, None
    if fam or spec:
        log.info("genre: off-vocabulary (%r, %r)", family, specific)
    return None, None


def family_of(name: object) -> str | None:
    """The family for a label that may be either a family or one of its specifics.

    Separate from :func:`normalise` because the callers differ. ``normalise``
    validates a pair the Director proposed and logs what it rejects, which is
    evidence. This one reads an outside label off a chart or a signal sheet,
    where a non-match is the ordinary case and not worth a line in the log.
    """
    slug = _slug(name)
    slug = ALIASES.get(slug, slug)
    if slug in SPECIFICS:
        return SPECIFICS[slug]
    return slug if slug in FAMILIES else None


def id3_name(family: object) -> str | None:
    """The TCON string for a family, or None.

    None rather than a fallback: an untagged track is honestly untagged, and a
    guessed TCON is a wrong shelf in every library that reads it.
    """
    return ID3_NAME.get(_slug(family))


def apple_family(genre_id: object) -> str | None:
    """Apple genreId -> family, or None for the umbrella and everything outside."""
    key = str(genre_id).strip() if genre_id is not None else ""
    if key in APPLE_UMBRELLA_IDS:
        return None
    return APPLE_GENRE_IDS.get(key)


def deezer_family(genre_id: object) -> str | None:
    """Deezer genre id -> family, or None for everything outside the roster."""
    key = str(genre_id).strip() if genre_id is not None else ""
    return DEEZER_GENRE_IDS.get(key)


# ── scoring ──────────────────────────────────────────────────────────────────
def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes where Postgres hands back aware ones."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _weight(rated_at: datetime | None, now: datetime) -> float:
    if rated_at is None:
        return 1.0
    rated_at = _aware(rated_at)
    age_days = max(0.0, (now - rated_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / TASTE_HALF_LIFE_DAYS)


def _blank_row(label: str, family: str) -> dict:
    return {"label": label, "family": family, "briefed": 0, "clips": 0,
            "shipped": 0, "qc_measured": 0, "qc_passed": 0,
            "rated_n": 0, "taste": None, "taste_raw": None,
            "producer": None, "reliability": None, "last_briefed": None}


def _finish(row: dict) -> dict:
    row["reliability"] = (round(row["qc_passed"] / row["qc_measured"], 3)
                          if row["qc_measured"] else None)
    row["taste"] = row["taste_raw"] if row["rated_n"] >= GENRE_MIN_RATED else None
    return row


def scores(*, now: datetime | None = None) -> dict:
    """Three numbers per label, reported separately and never collapsed.

    taste        the recency-weighted mean of Outcome.rating over DISTINCT
                 rated briefs. The only signal here not produced by the system
                 judging itself. Blank until GENRE_MIN_RATED.
    reliability  QC pass rate. Objective, available from day one, and not a
                 taste claim: it says which families burn credits, not which
                 ones are good.
    exposure     ``briefed``, the count of briefs. This is what the exploration
                 bonus consumes.

    Everything else a clip carries is excluded from taste on purpose.
    ``score_trend`` scores fit to the signal sheet, and the signal sheet is
    what chose the genre — Apple to genre_mix to Director to trend axis to
    codex to Director is a loop with no external input in it. ``score_hook``
    grades the Lyricist's work, and a genre that happened to get a good lyric
    is not a good genre. ``shipped`` is a rank within one day's field under an
    explicit diversity constraint, so rewarding it teaches the studio that rare
    genres are good. ``qc_verdict`` is a supplier fact: "Still (Nursery Without
    a Name)" was cut twice for truncated renders, and had genre been recorded
    then, the studio would have concluded that numb ballads are bad because
    Suno returned a 37-second file. The producer's own ``score_total`` is
    reported beside taste under its own heading and never blended into it.

    Counted over distinct BRIEFS, never clips, everywhere. One brief produces
    two clips carrying identical brief-derived fields, so four clip rows are
    two independent decisions — which is exactly how "no synths" and "no pads"
    reached the codex off two briefs. Applied to a controlled vocabulary that
    error would be worse, because a controlled label repeats by design: a
    family briefed twice in one day would otherwise clear the bar on that day's
    noise alone.

    No 60-day window, deliberately, unlike ``archivist.aggregate``. Stacking a
    decay on top of a cutoff means a rating counts fully at 59 days and not at
    all at 61, which is worse than either honest option and puts the retention
    rule in a second place. One number: the half-life.
    """
    now = now or _now()

    with session_scope() as s:
        brief_rows = s.execute(
            select(Brief.id, Brief.genre_family, Brief.genre, Run.run_date)
            .join(Run, Brief.run_id == Run.id)).all()
        clip_rows = s.execute(
            select(Clip.brief_id, Clip.genre_family, Clip.genre, Clip.shipped,
                   Clip.qc_verdict, Clip.score_total,
                   Outcome.rating, Outcome.rated_at)
            .outerjoin(Outcome, Outcome.clip_id == Clip.id)).all()

    families: dict[str, dict] = {f: _blank_row(f, f) for f in FAMILIES}
    specifics: dict[str, dict] = {}

    def rows_for(family: str | None, specific: str | None) -> list[dict]:
        out = []
        if family in families:
            out.append(families[family])
        if specific and specific in SPECIFICS:
            out.append(specifics.setdefault(
                specific, _blank_row(specific, SPECIFICS[specific])))
        return out

    for _bid, fam, spec, run_date in brief_rows:
        for row in rows_for(fam, spec):
            row["briefed"] += 1
            iso = run_date.isoformat() if run_date else None
            if iso and (row["last_briefed"] is None or iso > row["last_briefed"]):
                row["last_briefed"] = iso

    # Collapse each brief's rated clips to one sample before anything is
    # averaged. The two clips of a pair are one decision, not two.
    brief_label: dict[int, tuple[str | None, str | None]] = {}
    brief_ratings: dict[int, list[int]] = defaultdict(list)
    brief_rated_at: dict[int, datetime] = {}
    producer: dict[str, list[float]] = defaultdict(list)

    for bid, fam, spec, shipped, qc, total, rating, rated_at in clip_rows:
        brief_label[bid] = (fam, spec)
        for row in rows_for(fam, spec):
            row["clips"] += 1
            if shipped:
                row["shipped"] += 1
            if qc in ("pass", "fail"):
                row["qc_measured"] += 1
                if qc == "pass":
                    row["qc_passed"] += 1
            if total is not None:
                producer[row["label"]].append(float(total))
        if rating is not None:
            brief_ratings[bid].append(int(rating))
            if rated_at is not None:
                stamp = _aware(rated_at)
                prev = brief_rated_at.get(bid)
                if prev is None or stamp > prev:
                    brief_rated_at[bid] = stamp

    taste_acc: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for bid, ratings in brief_ratings.items():
        value = sum(ratings) / len(ratings)
        weight = _weight(brief_rated_at.get(bid), now)
        fam, spec = brief_label.get(bid, (None, None))
        for row in rows_for(fam, spec):
            row["rated_n"] += 1
            taste_acc[row["label"]].append((value, weight))

    for label, samples in taste_acc.items():
        total_w = sum(w for _v, w in samples)
        row = families.get(label) or specifics.get(label)
        if row is not None and total_w > 0:
            row["taste_raw"] = round(sum(v * w for v, w in samples) / total_w, 2)

    for label, vals in producer.items():
        row = families.get(label) or specifics.get(label)
        if row is not None and vals:
            row["producer"] = round(sum(vals) / len(vals), 2)

    for row in list(families.values()) + list(specifics.values()):
        _finish(row)

    rated_briefs = sum(1 for bid in brief_ratings
                       if brief_label.get(bid, (None, None))[0] in families)
    ranked = sorted(f for f, r in families.items() if r["taste"] is not None)

    return {
        "families": families,
        "specifics": dict(sorted(specifics.items())),
        "rated_briefs": rated_briefs,
        "ranked": ranked,
        "briefed_families": sorted(f for f, r in families.items() if r["briefed"]),
        "unlabelled_briefs": sum(1 for _b, fam, _s, _d in brief_rows if fam not in families),
    }


# ── regimes ──────────────────────────────────────────────────────────────────
def status(*, now: datetime | None = None, data: dict | None = None) -> dict:
    """Which regime the allocator is in, and one sentence saying why.

    Deliberately the same register as ``archivist.learning_status``: the two
    sit next to each other on the console and a reader should not have to work
    out whether they are measuring the same thing differently.
    """
    data = data or scores(now=now)
    ranked = data["ranked"]
    rated = data["rated_briefs"]

    if not ranked:
        regime = "cold"
        note = (f"cold — {rated} rated briefs and no family has reached "
                f"{GENRE_MIN_RATED}, so the slate is allocated for coverage and "
                f"nothing here is a preference")
    elif len(ranked) < GENRE_WARM_FAMILIES or rated < GENRE_WARM_TOTAL:
        regime = "thin"
        note = (f"thin — {len(ranked)} of {len(FAMILIES)} families have reached "
                f"{GENRE_MIN_RATED} rated briefs and {rated} briefs are rated "
                f"overall, against the {GENRE_WARM_FAMILIES} families and "
                f"{GENRE_WARM_TOTAL} briefs needed before preference steers the "
                f"day; one family gets one extra brief, no more")
    else:
        regime = "warm"
        note = (f"warm — {len(ranked)} families ranked over {rated} rated "
                f"briefs, so the slate is allocated by score with a permanent "
                f"exploration floor")

    return {"regime": regime, "note": note, "ranked": ranked,
            "rated_briefs": rated, "min_rated": GENRE_MIN_RATED,
            "warm_families": GENRE_WARM_FAMILIES, "warm_total": GENRE_WARM_TOTAL}


# ── external evidence ────────────────────────────────────────────────────────
def external_families(calibration: dict | None, external: dict | None) -> dict[str, list[str]]:
    """Which families today's outside feeds name, and who named each.

    External feeds pick the CANDIDATES; the ratings pick the winner. That
    division is the whole weighting rule, so this returns provenance and never
    a score. The only thing it can do to a slate is break a tie between two
    families the studio has sampled equally — recorded in the row's ``why`` so
    the influence is visible rather than inferred.

    ``external`` is the normalised counts written by the signal phase, shaped
    ``{"current": {family: n}, "catalogue": {family: n}}``. Only *current* is
    read here. Catalogue is 31 of 50 US entries on a typical day, largely
    back-catalogue riding a news cycle, and it says what was popular, not what
    is being released — blending the two is what turns a 6-6 tie between
    country and pop into a 23-6 rout.

    ``calibration`` is the Scout's ``sonic_calibration.genre_mix``, free text
    from a model. It is slugged and matched exactly; a name that does not match
    is ignored rather than guessed at, because reconciling an outside label
    with ours is judgement and belongs to the weekly retro, which proposes and
    never writes.
    """
    out: dict[str, list[str]] = defaultdict(list)

    current = (external or {}).get("current") if isinstance(external, dict) else None
    if isinstance(current, dict):
        for name, count in current.items():
            fam = family_of(name)
            if fam and count:
                out[fam].append("the current-release chart")

    mix = (calibration or {}).get("genre_mix") if isinstance(calibration, dict) else None
    for name in mix if isinstance(mix, list) else []:
        fam = family_of(name)
        if fam:
            out[fam].append("today's genre_mix")

    return {k: sorted(set(v)) for k, v in out.items()}


# ── allocation ───────────────────────────────────────────────────────────────
def _scaled(mean: float) -> float:
    """A 1-10 rating onto UCB1's [0,1] reward interval.

    Spelled out as its own function because UCB1's regret bound assumes rewards
    in [0,1], and a reader who sees 7.4 turn into 0.711 in a printed sum will
    otherwise assume a bug.
    """
    return (mean - 1.0) / 9.0


def _bonus(n: int, total: int) -> float:
    return UCB_C * math.sqrt(2.0 * math.log(max(total, 1)) / n)


def slate(n: int, *, calibration: dict | None = None, external: dict | None = None,
          explore_briefs: int | None = None, now: datetime | None = None) -> list[dict]:
    """How many of today's n briefs go to each family, and why.

    Counts, not an assignment. Which theme gets which genre is the Music
    Director's call — it is the only role that sees the emotional situation and
    the musical specification at the same time, and a 60-day aggregate has
    nothing whatever to say about today's ten themes. Same shape A&R already
    uses for personas: receive a cast, assign it yourself, have the counts
    enforced afterwards.

    Deterministic, and that is the point of choosing UCB1 over the
    alternatives. Epsilon-greedy's console explanation for an exploration pick
    is "a coin came up", which says nothing about why *that* family, and it
    spends a scarce brief re-testing a family already rated 3.1 over twenty
    songs as readily as one never tried. Thompson sampling usually has better
    regret but is stochastic, so the console could not answer "why this genre"
    with a number that would be the same if you asked twice. UCB1 is the only
    one of the three whose pick decomposes into two numbers a person can read
    off a screen: what we have seen, how unsure we are, and the sum that
    decided it.
    """
    if n <= 0:
        return []
    if explore_briefs is None:
        from .config import settings
        explore_briefs = settings().genre_explore_briefs

    data = scores(now=now)
    st = status(data=data)
    fams = data["families"]
    named = external_families(calibration, external)
    total_rated = data["rated_briefs"]

    # A plain dict and not a defaultdict: the membership checks below would
    # otherwise create a zero-count entry, and the slate would carry a row for
    # every family the allocator merely considered.
    picked: dict[str, int] = {}
    reason: dict[str, str] = {}
    stance: dict[str, str] = {}

    # Notional counts. Each pick increments them, so the next pick sees a
    # family it has already taken today as better sampled and less uncertain.
    # UCB1 picks one arm and the studio needs seven a day; this is what turns
    # the one-arm rule into a batch without inventing a second algorithm.
    n_hat = {f: fams[f]["rated_n"] for f in FAMILIES}
    total_hat = total_rated

    leader = _leader(fams)

    def take(family: str, why: str, how: str) -> None:
        picked[family] = picked.get(family, 0) + 1
        n_hat[family] += 1
        nonlocal total_hat
        total_hat += 1
        reason.setdefault(family, why)
        stance.setdefault(family, how)

    def under_cap() -> list[str]:
        free = [f for f in FAMILIES if picked.get(f, 0) < GENRE_MAX_PER_FAMILY]
        # Only when nine families times two would not cover the day. It cannot
        # today (18 > 7) but a config change is one edit away, and silently
        # returning a short slate would be worse than briefly exceeding a cap.
        return free or list(FAMILIES)

    floor = min(explore_briefs, n) if st["regime"] == "warm" else 0
    for _ in range(floor):
        pool = [f for f in under_cap() if f != leader]
        if not pool:
            break
        pick = min(pool, key=lambda f: (n_hat[f], fams[f]["briefed"], f))
        take(pick, f"exploration floor: {floor} of {n} briefs go to families the "
                   f"ratings have least to say about, whatever the leader scores",
             "explore")

    while sum(picked.values()) < n:
        pool = under_cap()
        if st["regime"] == "warm":
            pick, why, how = _ucb_pick(pool, fams, n_hat, total_hat, leader)
        else:
            pick, why, how = _coverage_pick(pool, fams, n_hat, named)
        take(pick, why, how)

    if st["regime"] == "thin":
        # A user who rates fifteen songs and sees the studio do exactly what it
        # did before concludes the rating control is decorative. One brief of
        # seven is the smallest response that is still a response, and it is
        # capped there because one ranked family is not evidence of a ranking.
        lead = _leader(fams, ranked_only=True)
        if lead and picked.get(lead, 0) < GENRE_MAX_PER_FAMILY:
            give_back = max((f for f in picked if f != lead),
                            key=lambda f: (picked[f], n_hat[f], f), default=None)
            if give_back:
                picked[give_back] -= 1
                if picked[give_back] == 0:
                    picked.pop(give_back)
                    reason.pop(give_back, None)
                    stance.pop(give_back, None)
                picked[lead] = picked.get(lead, 0) + 1
                n_hat[lead] += 1
                reason[lead] = (f"thin regime: one extra brief of {n} to the only "
                                f"ranked leader, at {fams[lead]['taste']:.1f} over "
                                f"{fams[lead]['rated_n']} rated briefs")
                stance[lead] = "exploit"

    out = []
    for family in sorted(picked, key=lambda f: (-picked[f], f)):
        row = fams[family]
        rated_n = row["rated_n"]
        mean = row["taste_raw"]
        scaled = _scaled(mean) if mean is not None else None
        bonus = _bonus(rated_n, max(total_rated, 1)) if rated_n else None
        # Rounded first, then added, so the three numbers the console prints
        # literally add up on screen rather than to within a rounding error.
        scaled = round(scaled, 6) if scaled is not None else None
        bonus = round(bonus, 6) if bonus is not None else None
        ucb = round(scaled + bonus, 6) if scaled is not None and bonus is not None else None
        why = reason.get(family, "coverage")
        if why is _UCB_WHY:
            why = (f"highest sum: {scaled:.3f} observed + {bonus:.3f} uncertainty "
                   f"= {ucb:.3f}")
        out.append({
            "genre_family": family,
            "specs": picked[family],
            "stance": stance.get(family, "explore"),
            "mean": mean,
            "n": rated_n,
            "mean_scaled": scaled,
            "bonus": bonus,
            "ucb": ucb,
            "ranked": row["taste"] is not None,
            "basis": _basis(row, named.get(family)),
            "why": why,
            "specifics": VOCABULARY[family],
        })
    return out


def _leader(fams: dict[str, dict], *, ranked_only: bool = False) -> str | None:
    """Highest mean among families that have cleared the bar. Ties alphabetically."""
    pool = [(f, r) for f, r in fams.items() if r["taste"] is not None]
    if not pool and not ranked_only:
        pool = [(f, r) for f, r in fams.items() if r["taste_raw"] is not None]
    if not pool:
        return None
    return min(pool, key=lambda kv: (-(kv[1]["taste_raw"] or 0.0), kv[0]))[0]


def _basis(row: dict, named: list[str] | None) -> str:
    if row["taste"] is not None:
        return f"your ratings ({row['rated_n']} rated briefs)"
    if row["rated_n"]:
        return (f"{row['rated_n']} rated briefs, below the {GENRE_MIN_RATED} "
                f"needed to rank")
    if row["briefed"]:
        return f"briefed {row['briefed']} times, not rated yet"
    if named:
        return "never briefed; named by " + " and ".join(named)
    return "never briefed"


def _coverage_pick(pool: list[str], fams: dict[str, dict], n_hat: dict[str, int],
                   named: dict[str, list[str]]) -> tuple[str, str, str]:
    """Least-sampled first. Round-robin, deterministic, no preference in it.

    This is not a placeholder for the bandit — it is UCB1's initialisation
    phase, which requires every arm pulled once before the bound means
    anything. Naming it that way makes the regimes one idea rather than two.

    A family named by today's outside feeds sorts ahead of one that is not,
    among families the studio has sampled equally. That is the only place an
    external chart touches an allocation, and it moves nothing that evidence
    has an opinion about.
    """
    pick = min(pool, key=lambda f: (n_hat[f], fams[f]["briefed"],
                                    0 if named.get(f) else 1, f))
    row = fams[pick]
    if not row["briefed"]:
        why = "coverage: never briefed"
    else:
        why = (f"coverage: {row['briefed']} briefs and {row['rated_n']} rated, "
               f"among the least-sampled families")
    if named.get(pick):
        why += " — and named by " + " and ".join(named[pick])
    return pick, why, "explore" if row["taste"] is None else "hold"


def _ucb_pick(pool: list[str], fams: dict[str, dict], n_hat: dict[str, int],
              total_hat: int, leader: str | None) -> tuple[str, str, str]:
    """The largest observed-plus-uncertainty sum, printed so it can be checked."""
    unsampled = [f for f in pool if n_hat[f] == 0]
    if unsampled:
        # UCB1 is undefined at n=0 and pulls every arm once first. Alphabetical
        # rather than arbitrary so two identical days produce identical slates.
        pick = min(unsampled)
        return pick, "never rated — UCB1 pulls every arm once before the bound means anything", "explore"

    best, best_score = None, None
    for f in sorted(pool):
        mean = fams[f]["taste_raw"]
        scaled = _scaled(mean) if mean is not None else 0.0
        score = scaled + _bonus(n_hat[f], total_hat)
        if best_score is None or score > best_score:
            best, best_score = f, score

    # The sentence is composed in slate() from the row's own printed columns,
    # not from the notional counts used to pick here. A reader checking the
    # arithmetic on screen has to be able to add up the numbers on screen, and
    # after the first pick of the day those two are no longer the same numbers.
    return best, _UCB_WHY, "exploit" if best == leader else "hold"


# ── enforcement ──────────────────────────────────────────────────────────────
def enforce(specs: list[dict], slate_rows: list[dict]) -> dict:
    """Normalise every spec's labels onto the vocabulary, then count.

    The enforcement is the normalisation: after this runs, the two columns hold
    a vocabulary term or NULL and nothing else, whatever the Director wrote.
    That is the guarantee the whole learning record rests on, and it is checked
    here rather than trusted to a prompt.

    A reader who knows ``anr._rebalance_personas`` will expect a breach of the
    cap to be fixed by swapping one spec to another family, and it deliberately
    is not. A persona is an assignment layered on top of a finished spec, so
    swapping it changes who sings. A genre is baked into the style_string the
    Director wrote for it — relabelling a country spec as folk would leave the
    prompt saying country and the learning record saying folk, which is worse
    than the imbalance it fixes. So a breach is recorded and logged, and the
    cap is held where it can be held honestly: at allocation time, in
    :func:`slate`.

    Nothing is ever dropped. A discarded spec costs a whole generation over a
    label. An off-slate genre with a stated reason is evidence about the slate,
    one without is noise, and a spec with no usable genre at all is written
    with NULLs and counted — that count is the evidence that the vocabulary is
    missing a term.

    Returns the ledger the console and ``run.notes["genre"]`` read.
    """
    wanted = {r["genre_family"]: r["specs"] for r in slate_rows}
    counts: dict[str, int] = defaultdict(int)
    off_vocabulary: list[str] = []
    off_slate: list[dict] = []

    for spec in specs:
        raw = spec.get("genre_family") or spec.get("genre")
        fam, label = normalise(spec.get("genre_family"), spec.get("genre"))
        spec["genre_family"], spec["genre"] = fam, label
        if fam is None:
            # Read before the write above lands, or the ledger records the null
            # it just wrote instead of the word the Director actually chose —
            # and that word is the whole evidence that a term is missing.
            off_vocabulary.append(str(raw or "")[:60])
            continue
        counts[fam] += 1
        if fam not in wanted:
            off_slate.append({
                "genre_family": fam,
                "reason": str(spec.get("genre_off_slate_reason") or "")[:160] or None,
            })

    over = {}
    for fam, got in counts.items():
        cap = max(wanted.get(fam, 0), GENRE_MAX_PER_FAMILY)
        if got > cap:
            over[fam] = {"asked": wanted.get(fam, 0), "got": got, "cap": cap}
            log.warning("genre: %d %s specs against a cap of %d", got, fam, cap)

    return {
        "asked": dict(sorted(wanted.items())),
        "got": dict(sorted(counts.items())),
        "over_cap": over,
        "off_slate": off_slate,
        "off_vocabulary": off_vocabulary,
        "unlabelled": len(off_vocabulary),
    }
