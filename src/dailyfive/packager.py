"""Packager — master, art, tags, delivery.

The audio decision worth stating explicitly, because it is the one thing here
that would be irreversible across a back catalogue: **the WAV is not
normalised.** It is trimmed and faded and otherwise left at the level it came
back at, because DSPs apply their own normalisation and delivering
pre-normalised discards a headroom decision that cannot be recovered. The
-14 LUFS pass goes on the MP3, which is the one that actually gets played.

The ``distribution`` block in every ``meta.json`` carries the fields a
distributor will one day ask for, all null. Nothing here fabricates an
identifier it has no authority to issue.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import unicodedata
from datetime import date
from pathlib import Path

from .config import settings
from .http import download
from .providers.modelark import ModelArkClient
from .qc import QCMetrics, have_ffmpeg, trim_points

log = logging.getLogger(__name__)

MP3_TARGET_LUFS = -14.0
MP3_TARGET_TP = -1.0
MP3_TARGET_LRA = 11.0
FADE_S = 0.03

# Present in every meta.json, populated by nothing. Reserving the shape costs a
# literal today; retrofitting it across a year of releases means touching 1,825
# folders.
DISTRIBUTION_TEMPLATE: dict = {
    "isrc": None, "iswc": None, "upc": None,
    "label": None, "publisher": None,
    "p_line": None, "c_line": None, "release_date": None,
    "primary_artist": None, "featured": [], "writers": [], "producers": [],
    "splits": [], "platform_ids": {},
    "explicit": None, "language": None,
    "primary_genre": None, "secondary_genre": None, "territories": None,
}

COVER_SYSTEM_PROMPT = (
    "Album cover artwork. Square composition, no text, no lettering, no words, "
    "no watermark, no logos. Bold and legible as a small thumbnail. "
)


def slugify(text: str, *, limit: int = 48) -> str:
    norm = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^\w\s-]", "", norm).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return (slug or "untitled")[:limit].rstrip("-")


# The delivery master carries no metadata at all, and it takes both of these to
# get there. `-map_metadata -1` drops what the source brought: Suno stamps every
# WAV it renders with an INFO comment naming itself, the render timestamp and
# the internal clip id, and ffmpeg copies that chunk forward by default, so a
# file handed to a distributor announced where it came from. `-bitexact` stops
# ffmpeg replacing it with its own ISFT software tag — without it the LIST chunk
# is emptied and then immediately rewritten with the encoder version, which is
# the confusing half: the strip appears not to have worked.
#
# The MP3 is tagged on purpose and is not covered by this. A player needs a
# title and an artist; a delivery master needs nothing, and the WAV is the one
# that gets ingested by something automated.
WAV_NO_METADATA = ["-map_metadata", "-1", "-bitexact"]


def master_wav(source: Path, dest: Path, metrics: QCMetrics) -> Path:
    """Trim, fade, and otherwise leave the level alone.

    Deliberately not a normalisation step. See the module docstring.
    """
    _need_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    start, end = trim_points(metrics)

    filters = [f"afade=t=in:st=0:d={FADE_S}"]
    if end is not None:
        length = max(0.5, end - start)
        filters.append(f"afade=t=out:st={max(0.0, length - FADE_S):.3f}:d={FADE_S}")

    cmd = ["ffmpeg", "-y", "-nostats", "-hide_banner"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if end is not None:
        cmd += ["-to", f"{max(0.5, end - start):.3f}"]
    cmd += ["-af", ",".join(filters), "-c:a", "pcm_s16le", "-ar", "44100"]
    cmd += WAV_NO_METADATA + [str(dest)]

    _run(cmd, f"mastering {dest.name}")
    return dest


def encode_mp3(source: Path, dest: Path) -> Path:
    """320 kbps, two-pass loudnorm to -14 LUFS.

    Two passes rather than one because single-pass loudnorm guesses at the
    programme loudness and can overshoot by several LU on a dynamic track.
    """
    _need_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    measured = _measure_loudnorm(source)

    af = (f"loudnorm=I={MP3_TARGET_LUFS}:TP={MP3_TARGET_TP}:LRA={MP3_TARGET_LRA}"
          f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
          f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
          f":offset={measured['target_offset']}:linear=true:print_format=summary"
          ) if measured else (
        f"loudnorm=I={MP3_TARGET_LUFS}:TP={MP3_TARGET_TP}:LRA={MP3_TARGET_LRA}")

    _run(["ffmpeg", "-y", "-nostats", "-hide_banner", "-i", str(source),
          "-af", af, "-c:a", "libmp3lame", "-b:a", "320k", "-ar", "44100", str(dest)],
         f"encoding {dest.name}")
    return dest


def _measure_loudnorm(source: Path) -> dict | None:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostats", "-hide_banner", "-i", str(source),
             "-af", f"loudnorm=I={MP3_TARGET_LUFS}:TP={MP3_TARGET_TP}"
                    f":LRA={MP3_TARGET_LRA}:print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=600, check=False)
    except Exception as exc:
        log.warning("loudnorm measurement failed (%s) — falling back to single pass", exc)
        return None

    blob = re.search(r"\{[^{}]*\"input_i\".*?\}", proc.stderr or "", re.DOTALL)
    if not blob:
        return None
    try:
        return json.loads(blob.group(0))
    except json.JSONDecodeError:
        return None


def write_lrc(aligned_words: list[dict], title: str, artist: str | None = None) -> str:
    """Turn word-level alignment into a line-level .lrc.

    Suno aligns per word; a karaoke file with one word per timestamp is
    unreadable, so words are grouped into lines on the gaps between them.
    """
    lines: list[str] = [f"[ti:{title}]"]
    if artist:
        lines.append(f"[ar:{artist}]")
    lines.append("[by:The Daily Five]")

    current: list[str] = []
    line_start: float | None = None
    prev_end: float | None = None

    def flush() -> None:
        nonlocal current, line_start
        if current and line_start is not None:
            lines.append(f"[{_stamp(line_start)}]{' '.join(current).strip()}")
        current, line_start = [], None

    for w in aligned_words:
        word = str(w.get("word") or "").strip()
        if not word:
            continue
        start = _f(w.get("startS"))
        if start is None:
            continue
        end = _f(w.get("endS"))

        # The break belongs *before* the word that follows the pause, not after
        # it — otherwise every line starts one word late.
        gap = (start - prev_end) if prev_end is not None else 0.0
        too_long = len(" ".join(current + [word])) > 46
        if current and (gap > 0.9 or too_long):
            flush()

        if line_start is None:
            line_start = start
        current.append(word)
        prev_end = end if end is not None else start

    flush()
    return "\n".join(lines) + "\n"


def _stamp(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m):02d}:{s:05.2f}"


def cover_art(brief: dict, dest: Path, *, client: ModelArkClient | None = None) -> Path | None:
    """3000x3000 — the size every DSP accepts, so it never needs regenerating."""
    if client is None and not settings().ark_api_key:
        # ModelArkClient's constructor raises before it opens a socket when the
        # key is unset, so without this the code asks for a client it already
        # knows cannot exist and then logs its own objection at ERROR, five
        # times a day forever. An unconfigured optional integration is a
        # settled fact about the deployment, not an event worth alerting on;
        # the caller says so once per run instead. The check is skipped when a
        # client was handed in, because an injected client is the caller
        # asserting it has one.
        return None
    try:
        client = client or ModelArkClient()
        prompt = _cover_prompt(brief)
        url = client.cover(prompt)
        download(url, dest, provider="modelark-image")
        return dest
    except Exception as exc:
        # Still ERROR, and deliberately: a key that is configured and stops
        # working is a real event. Art is the one deliverable worth shipping
        # without — a missing cover is a gap in a folder, a failed run is a
        # missing day. ProviderError subclasses DailyFiveError subclasses
        # Exception, so the tuple this replaces only ever meant Exception.
        log.error("cover art failed for %r: %s", brief.get("title"), exc)
        return None


def _cover_prompt(brief: dict) -> str:
    bits = [COVER_SYSTEM_PROMPT]
    dv = brief.get("diversity_vector") or {}
    if brief.get("theme"):
        bits.append(f"Evoking, without depicting literally: {brief['theme']}.")
    if dv.get("mood"):
        bits.append(f"Mood: {dv['mood']}.")
    if brief.get("instrumentation"):
        bits.append(f"Sonic character: {brief['instrumentation']}.")
    bits.append("Strong single focal point, high contrast, confident colour palette.")
    return " ".join(bits)[:1500]


def tag_mp3(path: Path, *, title: str, artist: str, album: str, year: str,
            genre: str | None = None, cover: Path | None = None,
            lyrics: str | None = None, comment: str | None = None) -> None:
    from mutagen.id3 import (APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1,
                             USLT, ID3NoHeaderError)
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()

    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags["TPE1"] = TPE1(encoding=3, text=artist)
    tags["TALB"] = TALB(encoding=3, text=album)
    tags["TDRC"] = TDRC(encoding=3, text=year)
    if genre:
        tags["TCON"] = TCON(encoding=3, text=genre)
    if comment:
        tags["COMM"] = COMM(encoding=3, lang="eng", desc="", text=comment)
    if lyrics:
        tags["USLT"] = USLT(encoding=3, lang="eng", desc="", text=lyrics)
    if cover and cover.is_file():
        tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover",
                            data=cover.read_bytes())
    tags.save(str(path), v2_version=3)


def build_meta(clip: dict, brief: dict, run_date: date, slot_index: int,
               qc: dict, *, cover: Path | None = None) -> dict:
    """The per-song meta.json, distribution block included and empty.

    ``cover`` is the file that was actually produced, or None. A manifest that
    names cover.jpg unconditionally is a machine-readable false statement three
    lines above a block that goes to the trouble of reserving fields it has no
    authority to fill. The key stays, with nothing in it, for the same reason
    isrc does.
    """
    return {
        "title": clip.get("title") or brief.get("title"),
        "slug": f"{slot_index:02d}_{slugify(clip.get('title') or brief.get('title', ''))}",
        "run_date": run_date.isoformat(),
        "slot": {"type": clip.get("slot_type"), "index": slot_index},
        "persona": {
            "name": brief.get("persona_name"),
            "id": brief.get("persona_id"),
            "model": brief.get("persona_model"),
        },
        "musical": {
            # Here and deliberately not in the distribution block, whose rule
            # is that all of it stays null and whose value comes from having no
            # exceptions. A distributor does want a genre at delivery, but from
            # its own controlled vocabulary — DistroKid's, TuneCore's and CD
            # Baby's lists are all different and none of them is ours — so
            # filling primary_genre from this label would assert a mapping we
            # have no authority to make, in the one block built not to do that.
            # Recording it here is also what finally makes ARCHITECTURE.md:264
            # true: it claimed genre was "already recorded elsewhere in the same
            # file" when it appeared nowhere but inside the null block itself.
            "genre_family": brief.get("genre_family"),
            "genre": brief.get("genre"),
            "bpm": brief.get("bpm"),
            "key": brief.get("musical_key") or brief.get("key"),
            "song_form": brief.get("song_form"),
            "instrumentation": brief.get("instrumentation"),
            "duration_s": clip.get("duration_s"),
            "vocal_gender": clip.get("vocal_gender"),
        },
        "brief": {
            "id": brief.get("id"),
            "theme": brief.get("theme"),
            "angle": brief.get("angle"),
            "diversity_vector": brief.get("diversity_vector"),
        },
        "generation": {
            "provider": "sunoapi.org",
            "model": clip.get("model"),
            "task_id": clip.get("task_id"),
            "audio_id": clip.get("audio_id"),
            "style": clip.get("style_string"),
            "negative_tags": clip.get("negative_tags"),
            "style_weight": clip.get("style_weight"),
            "weirdness_constraint": clip.get("weirdness"),
            "audio_weight": clip.get("audio_weight"),
            "tags": clip.get("tags"),
        },
        "qc": qc,
        "mastering": {
            "wav": "trimmed and faded only — not loudness normalised, "
                   "so it stays a true delivery master",
            "mp3": f"320 kbps, loudness normalised to {MP3_TARGET_LUFS} LUFS "
                   f"/ {MP3_TARGET_TP} dBTP",
        },
        "producer": {
            "score_hook": clip.get("score_hook"),
            "score_mix": clip.get("score_mix"),
            "score_trend": clip.get("score_trend"),
            "score_total": clip.get("score_total"),
            "why": clip.get("why"),
        },
        "files": {
            "wav": "master.wav", "mp3": "master.mp3",
            "cover": cover.name if cover else None,
            "lyrics_txt": "lyrics.txt", "lyrics_lrc": "lyrics.lrc",
        },
        # Reserved, deliberately empty. See the module docstring.
        "distribution": json.loads(json.dumps(DISTRIBUTION_TEMPLATE)),
    }


def _need_ffmpeg() -> None:
    if not have_ffmpeg():
        raise RuntimeError(
            "ffmpeg is required for mastering and encoding. "
            "Install with `apt-get install -y ffmpeg`.")


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise RuntimeError(f"{what} failed:\n" + "\n".join(tail))


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
