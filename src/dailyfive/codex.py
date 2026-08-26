"""The Style Codex — the thing that makes the Music Director more than a prompt.

Two rules govern what goes in here.

Specifications, not adjectives. "Moody" is not a brief; "84 BPM, F minor, hook
at 0:07, sub-heavy, no snare until bar 9" is something the Compiler can turn
into a payload and the QC Engineer can check the result against.

Characteristics, never names. Encoding "in the style of [living artist]" gets
the generation rejected by Suno's own filter and is not defensible anyway. The
codex describes production techniques, eras and instrumentation instead.

Versioned, never edited in place: a regression traces back to the edit that
caused it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from .db import session_scope
from .models import CodexVersion

log = logging.getLogger(__name__)

SEED_CODEX: dict[str, Any] = {
    "tempo_bands": {
        "ballad": [60, 78],
        "midtempo": [79, 104],
        "uptempo": [105, 128],
        "club": [129, 150],
    },
    "song_forms": {
        "full": [
            "Intro(4) - Verse(16) - Pre(8) - Chorus(16) - Verse(16) - Pre(8) - "
            "Chorus(16) - Bridge(8) - Chorus(16) - Outro(4)",
            "Hook(8) - Verse(16) - Hook(8) - Verse(16) - Hook(16) - Bridge(8) - Hook(16)",
            "Intro(8) - Verse(16) - Chorus(16) - Verse(16) - Chorus(16) - "
            "Post-chorus(8) - Chorus(16)",
        ],
        "short": [
            "Hook(8) - Verse(8) - Hook(8) - Hook(8)",
            "Intro(2) - Hook(8) - Verse(8) - Hook(8)",
        ],
    },
    "hook_placement": {
        "full": "first chorus lands by 0:35; a recognisable motif inside the first 12s",
        "short": "hook inside the first 2 seconds; loop seam must be inaudible",
    },
    "mix_targets": {
        "vocal_register": "sits 3-6 dB above the bed; no masking below 200 Hz",
        "low_end": "sub fundamental 40-60 Hz, mono below 120 Hz",
        "dynamics": "chorus 2-4 LU louder than verse",
    },
    "instrumentation_palettes": [
        "sub bass, tight closed hats, fingersnap, muted rhodes, airy pad",
        "acoustic guitar, brushed kit, upright bass, room reverb, double-tracked vocal",
        "analog saw lead, sidechained pad, four-on-floor kick, white-noise riser",
        "detuned rhodes, dusty break, vinyl crackle, upright piano, tape saturation",
        "gated reverb snare, DX7 bell, palm-muted guitar, arpeggiated bass",
    ],
    "negative_defaults": "lo-fi, muddy mix, off-key vocals, clipping, abrupt ending",
    "learned": {
        # Populated by the Archivist. Empty on day one and that is honest —
        # nothing has been observed yet.
        "style_scores": {},
        # {family: {"mean": 7.4, "n": 12}}, and the n travels with the mean
        # rather than being dropped on the way in. The live codex reached v3
        # showing "no synths 6.11" with no sample count anywhere on the row,
        # which is how an average over two decisions came to read as a finding.
        # A genre label repeats by construction, so a number here will look
        # solid long before it is, and the only defence is printing what it
        # rests on everywhere it is printed at all.
        "genre_scores": {},
        "subgenre_scores": {},
        "bpm_scores": {},
        "avoid": [],
        "notes": [],
    },
}

SEED_PERSONAS: list[dict[str, Any]] = [
    {
        "name": "Vale",
        # Filled by `dailyfive personas bootstrap`, which creates a STYLE persona
        # via /generate-persona. voice_persona is a different artefact entirely —
        # it needs a voiceId from the Suno Voice workflow, not a personaId.
        "persona_id": None,
        "persona_model": "style_persona",
        "vocal_gender": "f",
        "territory": "restrained alt-pop; close-mic vocal, minimal percussion, "
                     "space as an instrument",
        "tempo_pref": "ballad|midtempo",
        "lyric_voice": "first person, present tense, concrete domestic detail",
    },
    {
        "name": "Rook",
        "persona_id": None,
        "persona_model": "style_persona",
        "vocal_gender": "m",
        "territory": "low-slung electronic soul; heavy sub, sparse hats, "
                     "processed backing stacks",
        "tempo_pref": "midtempo",
        "lyric_voice": "second person, direct address, short declarative lines",
    },
    {
        "name": "Marisol",
        "persona_id": None,
        "persona_model": "style_persona",
        "vocal_gender": "f",
        "territory": "warm uptempo with live-band feel; real kit, horn stabs, "
                     "call-and-response",
        "tempo_pref": "uptempo|club",
        "lyric_voice": "communal, plural pronouns, refrain-led",
    },
]


# A style fragment that begins with one of these describes what is ABSENT, and
# an absence cannot be rendered in either direction without inverting it. The
# live codex reached v3 with a learned table consisting entirely of "no synths"
# 6.11 and "no pads" 6.11 — two negations, scraped out of prose, about to be
# handed to the Music Director under a heading saying they were observed to
# score well and an instruction saying such observations beat its priors. One
# more run and the studio would have taught itself that the measured route to a
# good song is removing instruments. The mirror image is no better: the same
# token under "observed to fail, avoid" reads as avoid avoiding synths.
#
# So a negation is not a token that scored badly, it is a token that cannot be
# scored. It is dropped where it is collected and again where it is rendered —
# twice, because the live codex already holds two of them and a filter at the
# tokeniser alone would still brief tomorrow's run from yesterday's table.
#
# "non-" is deliberately absent from the list. "non-quantised drums" and
# "non-diegetic pad" name what a thing IS, by contrast, and they are scoreable;
# the prefixes below all introduce something that is simply not there.
NEGATION_PREFIXES = ("no ", "not ", "never ", "without ", "avoid ",
                     "minus ", "zero ", "free of ", "absent ", "lacking ")


def is_negation(token: str) -> bool:
    """Whether a style fragment states an absence rather than a characteristic."""
    tok = (token or "").strip().lower()
    return tok.startswith(NEGATION_PREFIXES) or tok.endswith(("-free", " free"))


def scored(entry: Any) -> tuple[float, int] | None:
    """A genre score row as (mean, n), or None if it carries neither.

    Tolerant because the body is JSON read back out of the database and a codex
    written by an older deploy is still a valid codex — versions are never
    rewritten in place, so every shape this key has ever had stays readable
    forever. A bare number is one of those older shapes; it renders with n=0,
    which is exactly what a mean with no sample count behind it is worth.

    Public, and the console reads the stored body through this and nothing
    else. A page that parses the shape itself will one day be stricter than
    the prompt path is, and then a body the Director briefs from fine is a
    body the console 500s on — with no way to fix it, because a codex version
    is never rewritten in place.
    """
    if isinstance(entry, dict):
        mean, n = entry.get("mean"), entry.get("n")
        if isinstance(mean, (int, float)):
            return float(mean), int(n or 0)
        return None
    if isinstance(entry, (int, float)):
        return float(entry), 0
    return None


def _genre_line(scores: Any, label: str, limit: int = 8) -> str | None:
    """One prompt line of genre scores, each printed with what it rests on.

    The sample count is not decoration and is not optional. director.py tells
    the model that a learned observation beats its priors, so a number arriving
    without its n is a number the model has no way to discount — and a genre
    label, unlike a prose fragment, repeats by construction and will reach a
    confident-looking average from a handful of days.
    """
    rows = []
    for key, entry in (scores or {}).items():
        pair = scored(entry)
        if pair:
            rows.append((key, *pair))
    if not rows:
        return None
    rows.sort(key=lambda r: -r[1])
    return f"{label}: " + ", ".join(
        f"{k} {mean:.1f} over {n} rated brief{'' if n == 1 else 's'}"
        for k, mean, n in rows[:limit])


@dataclass(slots=True)
class Codex:
    version: int
    body: dict[str, Any]
    personas: list[dict[str, Any]]

    def personas_ready(self) -> list[dict[str, Any]]:
        """Only personas that have actually been created on Suno's side.

        A brief assigned a persona with no ``persona_id`` would silently render
        as a generic voice, which defeats the point of having a cast.
        """
        return [p for p in self.personas if p.get("persona_id")]

    def form_for(self, slot_type: str, idx: int) -> str:
        forms = self.body.get("song_forms", {}).get(slot_type) or ["Verse - Chorus - Verse - Chorus"]
        return forms[idx % len(forms)]

    def palette_for(self, idx: int) -> str:
        palettes = self.body.get("instrumentation_palettes") or [""]
        return palettes[idx % len(palettes)]

    def tempo_band(self, name: str) -> tuple[int, int]:
        band = self.body.get("tempo_bands", {}).get(name)
        if not band:
            return (80, 120)
        return (int(band[0]), int(band[1]))

    def avoid_list(self) -> list[str]:
        return list(self.body.get("learned", {}).get("avoid") or [])

    def negative_tags(self) -> str:
        base = self.body.get("negative_defaults", "")
        avoid = self.avoid_list()
        return ", ".join(filter(None, [base, ", ".join(avoid)]))[:900]

    def brief_context(self, *, slots: Sequence[str] | None = None) -> str:
        """A compact rendering for prompts. Full JSON would be mostly noise.

        `slots` narrows the per-lane guidance to the lanes actually being briefed.
        The codex records what the studio knows how to make; a run asks for a
        subset of that, and hook placement for a lane with no slots is guidance
        for a spec that would be thrown away.
        """
        hooks = self.body.get("hook_placement", {})
        if slots is not None:
            hooks = {k: v for k, v in hooks.items() if k in slots}
        learned = self.body.get("learned", {})
        parts = [
            f"tempo bands: {json.dumps(self.body.get('tempo_bands', {}))}",
            f"hook placement: {json.dumps(hooks)}",
            f"mix targets: {json.dumps(self.body.get('mix_targets', {}))}",
            f"palettes: {json.dumps(self.body.get('instrumentation_palettes', [])[:6])}",
        ]
        scored = {k: v for k, v in (learned.get("style_scores") or {}).items()
                  if not is_negation(k)}
        if scored:
            top = sorted(scored.items(), key=lambda kv: -kv[1])[:8]
            parts.append("observed to score well: " + ", ".join(f"{k} ({v:.1f})" for k, v in top))
        for line in (_genre_line(learned.get("genre_scores"),
                                 "genre observed, your ratings over distinct briefs"),
                     _genre_line(learned.get("subgenre_scores"),
                                 "specific genres observed", limit=6)):
            if line:
                parts.append(line)
        avoid = [k for k in (learned.get("avoid") or []) if not is_negation(k)]
        if avoid:
            parts.append("observed to fail, avoid: " + ", ".join(avoid[:10]))
        if learned.get("notes"):
            parts.append("director notes: " + " | ".join(learned["notes"][-5:]))
        return "\n".join(parts)


def current() -> Codex:
    """Latest codex, seeding version 1 on first call."""
    with session_scope() as s:
        row = s.execute(
            select(CodexVersion).order_by(CodexVersion.version.desc()).limit(1)
        ).scalar_one_or_none()
        if row is None:
            row = CodexVersion(version=1, body=SEED_CODEX, personas=SEED_PERSONAS,
                               rationale="seed codex — no observations yet")
            s.add(row)
            s.flush()
            log.info("seeded codex v1 with %d personas", len(SEED_PERSONAS))
        return Codex(version=row.version, body=dict(row.body), personas=list(row.personas))


def save_new_version(body: dict, personas: list[dict], *, diff: str, rationale: str) -> int:
    """Append a version. Never mutates the row it was derived from."""
    with session_scope() as s:
        latest = s.execute(
            select(CodexVersion).order_by(CodexVersion.version.desc()).limit(1)
        ).scalar_one_or_none()
        version = (latest.version if latest else 0) + 1
        s.add(CodexVersion(version=version, body=body, personas=personas,
                           diff=diff, rationale=rationale))
        log.info("codex v%d written: %s", version, rationale[:120])
        return version


def set_persona_id(name: str, persona_id: str,
                   persona_model: str = "style_persona") -> int:
    """Record a persona created on Suno's side. Returns the new codex version."""
    cx = current()
    personas = [dict(p) for p in cx.personas]
    found = False
    for p in personas:
        if p.get("name") == name:
            p["persona_id"] = persona_id
            p["persona_model"] = persona_model
            found = True
    if not found:
        personas.append({"name": name, "persona_id": persona_id,
                         "persona_model": persona_model, "vocal_gender": None,
                         "territory": "", "tempo_pref": "midtempo", "lyric_voice": ""})
    return save_new_version(cx.body, personas, diff=f"persona {name} -> {persona_id}",
                            rationale=f"registered persona {name}")
