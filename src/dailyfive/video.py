"""Video assembly — the deterministic half of the video pipeline.

Two deliverables with almost nothing in common except the .lrc the Packager
already writes.

The LYRIC VIDEO is the whole song, 16:9, and it costs nothing per song: cover
art, timed text, ffmpeg. No generative video is involved and none is wanted —
the format's entire job is to be legible for four minutes on a television, and
a generated clip that loops for four minutes is worse than a still that does
not pretend to be moving.

The HOOK SHORT is 9:16, about twenty seconds, and it is the expensive one: a
handful of Seedance clips of one dancer, cut on the beat, with the hook lyric
burned over the top. That module is separate; this one takes whatever clips
arrive and assembles them.

Text is rendered through libass rather than drawtext. drawtext takes one string
per filter and would need sixty-four chained filters for a five-minute song,
each with its own enable=between(t,...) expression; libass takes one file and
does the timing itself, which is what subtitle formats exist for.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# A line is on screen until the next one starts. The last line has no successor,
# so it gets this. Long enough to read, short enough not to hang over the outro.
LAST_LINE_S = 4.0

# Below this, two stamps are the same moment written twice — the .lrc format
# allows repeated stamps on one lyric and the Packager emits them. Merging is
# wrong (the words differ); showing both is wrong (they collide); so the second
# one is nudged rather than dropped.
MIN_GAP_S = 0.35

FONT = "DejaVu Sans"          # verified present in the container; see Dockerfile

_STAMP = re.compile(r"\[(\d{1,2}):(\d{2}(?:\.\d+)?)\]")
_SECTION = re.compile(r"\[([A-Za-z][A-Za-z ]{1,20}\d?)\]")
_METADATA = re.compile(r"^\[(ti|ar|al|by|offset|re|ve):", re.I)


@dataclass
class Line:
    start: float
    end: float
    text: str
    section: str | None = None


def parse_lrc(text: str) -> list[Line]:
    """Timed lines from the .lrc the Packager writes.

    Handles the two shapes that file actually contains, both visible in a real
    delivery: a stamp followed by its lyric on the SAME line, and a stamp on its
    own followed by the lyric on the NEXT line. A parser that only handles the
    first silently returns half the song.
    """
    lines: list[Line] = []
    pending: float | None = None
    section: str | None = None

    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or _METADATA.match(raw):
            continue

        stamps = _STAMP.findall(raw)
        body = _STAMP.sub("", raw).strip()

        # A section marker sits either alone on a line or, more often, welded to
        # the end of the lyric that precedes it: "[01:20.31]in two days [Pre]".
        # Matching only the standalone form leaves the tag inside the lyric, so
        # the words "[Pre]" get burned into the video, and every section after
        # the second is never detected — which collapses a nine-slide song into
        # three and puts a still image on screen for most of a verse.
        trailing = _SECTION.search(body)
        next_section = None
        if trailing and trailing.end() == len(body):
            next_section = trailing.group(1).strip()
            body = body[:trailing.start()].strip()

        if next_section and not body:
            section = next_section
            if stamps:
                m, s = stamps[-1]
                pending = int(m) * 60 + float(s)
            continue

        if stamps:
            # Repeated stamps on one lyric mean the line recurs; the last is the
            # one that matters for a first-pass render.
            m, s = stamps[-1]
            at = int(m) * 60 + float(s)
            if body:
                lines.append(Line(at, at + LAST_LINE_S, body, section))
                pending = None
            else:
                pending = at
        elif body and pending is not None:
            lines.append(Line(pending, pending + LAST_LINE_S, body, section))
            pending = None
        if next_section:
            # Applies to what follows, not to the line it was welded onto.
            section = next_section

    lines.sort(key=lambda ln: ln.start)
    for i, ln in enumerate(lines[:-1]):
        nxt = lines[i + 1].start
        ln.end = nxt if nxt - ln.start >= MIN_GAP_S else ln.start + MIN_GAP_S
    return lines


def _ass_escape(s: str) -> str:
    # A brace opens an override block in ASS, and a lyric containing one would
    # silently swallow the rest of the line rather than print it.
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(lines: list[Line], *, width: int, height: int,
              size: int | None = None, margin_v: int | None = None) -> str:
    """One subtitle file, styled for burning in.

    Sized from the frame rather than fixed, because the same function serves a
    1080p landscape lyric video and a 1080x1920 short, and a point size that
    reads on one is unreadable on the other.
    """
    size = size or int(height * 0.058)
    margin_v = margin_v or int(height * 0.12)
    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour,"
        " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
        " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # &H00FFFFFF is opaque white; &HC8000000 is black at ~78% alpha behind it.
        # An outline alone fails over a bright cover, and a solid box fails over a
        # dark one — the shadowed outline survives both.
        f"Style: Lyric,{FONT},{size},&H00FFFFFF,&H00000000,&HC8000000,"
        f"-1,0,0,0,100,100,0,0,1,{max(2, size // 18)},{max(1, size // 26)},"
        f"2,80,80,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *[f"Dialogue: 0,{_ts(ln.start)},{_ts(ln.end)},Lyric,,0,0,0,,"
          f"{{\\fad(180,180)}}{_ass_escape(ln.text)}"
          for ln in lines],
        "",
    ])


def _ffmpeg(args: list[str], what: str) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not on PATH")
    # -nostdin or ffmpeg eats the caller's stdin. A shell loop feeding paths
    # from a heredoc then runs a fraction of its iterations and reports no
    # error at all — the first assembly built an eight-cut short from three.
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-nostats", "-hide_banner", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed: {r.stderr.strip()[-600:]}")


def lyric_video(audio: Path, lrc: Path, dest: Path, *,
                cover: Path | None = None, duration_s: float | None = None,
                width: int = 1920, height: int = 1080) -> Path:
    """The full song as a 16:9 lyric video. No generative video, no per-song cost.

    The cover is used twice: blurred and darkened as a bed so white text is
    legible whatever the artwork does, and sharp in the centre so the record
    still has a face. A slow zoom on the bed keeps it from reading as a frozen
    stream, which is what a still image on YouTube looks like after ten seconds.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = parse_lrc(lrc.read_text(encoding="utf-8"))
    if not lines:
        raise RuntimeError(f"no timed lines in {lrc}")

    ass = dest.with_suffix(".ass")
    ass.write_text(build_ass(lines, width=width, height=height), encoding="utf-8")

    dur = duration_s or (lines[-1].end + 3.0)
    art = cover if cover and cover.is_file() else None

    if art:
        # zoompan runs on a frame sequence, so the still is looped into one first.
        # d=1 with a per-frame zoom is what makes it a move rather than a slideshow.
        chain = (
            f"[0:v]scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
            f"crop={width * 2}:{height * 2},"
            f"zoompan=z='min(zoom+0.00008,1.12)':d=1:s={width}x{height}:fps=25,"
            f"boxblur=luma_radius=28:luma_power=2,eq=brightness=-0.16:saturation=0.85[bed];"
            f"[0:v]scale={int(height * 0.42)}:{int(height * 0.42)}[art];"
            f"[bed][art]overlay=(W-w)/2:{int(height * 0.16)}[bg];"
            f"[bg]subtitles='{ass.as_posix()}'[v]"
        )
        _ffmpeg(["-loop", "1", "-i", str(art), "-i", str(audio),
                 "-filter_complex", chain, "-map", "[v]", "-map", "1:a",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                 "-t", f"{dur:.2f}", "-movflags", "+faststart", str(dest)],
                f"lyric video for {dest.name}")
    else:
        _ffmpeg(["-f", "lavfi", "-i", f"color=c=0x0E1317:s={width}x{height}:r=25",
                 "-i", str(audio), "-vf", f"subtitles='{ass.as_posix()}'",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                 "-t", f"{dur:.2f}", "-movflags", "+faststart", str(dest)],
                f"lyric video for {dest.name}")
    ass.unlink(missing_ok=True)
    return dest


def hook_window(lines: list[Line], *, want_s: float = 22.0) -> tuple[float, float]:
    """Where the short should start and stop.

    The first chorus, because that is where the hook is and a short has about
    two seconds to earn the next twenty. Falls back to the loudest-labelled
    section available, then to a point a third of the way in — never to zero,
    which on these songs is an intro nobody stays for.
    """
    if not lines:
        return 0.0, want_s
    for want in ("Chorus", "Hook", "Drop"):
        hit = next((ln for ln in lines if (ln.section or "").startswith(want)), None)
        if hit:
            start = max(0.0, hit.start - 0.35)
            return start, start + want_s
    start = lines[len(lines) // 3].start
    return start, start + want_s


# ── the short ────────────────────────────────────────────────────────────────
# Text on a short has to survive two things a lyric video's text does not: a
# busy moving frame behind it, and a platform re-encode that is far harsher than
# anything here. Both are handled the same way — the text is composited last, at
# the output's own resolution, and never touched again.
#
# The ordering is the whole trick. Every scale, crop, pad and concat happens
# BEFORE the subtitle filter, so glyphs are rasterised once at 1080x1920 and go
# straight into the encoder. Burning text first and scaling after resamples the
# glyph edges and is what makes overlaid captions look soft; it is also the
# default shape of the naive filter chain, which is why it happens so often.
SHORT_W, SHORT_H = 1080, 1920


def hook_short(clips: list[Path], audio: Path, lrc: Path, dest: Path, *,
               start_s: float, duration_s: float = 22.0,
               bpm: int | None = None) -> Path:
    """Assemble generated clips into a 9:16 short with the hook lyric over it.

    The clips carry no sound — Seedance does not generate any — so the master's
    own hook window is the audio bed, which is also why the cut can be exact:
    the beat grid comes from the brief's BPM rather than from analysing a
    render, so a cut lands on the beat by construction instead of by luck.
    """
    if not clips:
        raise RuntimeError("no clips to assemble")
    dest.parent.mkdir(parents=True, exist_ok=True)

    lines = [ln for ln in parse_lrc(lrc.read_text(encoding="utf-8"))
             if start_s <= ln.start < start_s + duration_s]
    for ln in lines:
        ln.start -= start_s
        ln.end = min(ln.end - start_s, duration_s)

    ass = dest.with_suffix(".ass")
    # Bigger and heavier than the landscape style: a phone is held at arm's
    # length and the caption competes with a moving body behind it. Outline, not
    # a box — a box reads as a subtitle track, an outline reads as a caption,
    # and captions are what this format uses.
    ass.write_text(build_ass(lines, width=SHORT_W, height=SHORT_H,
                             size=int(SHORT_H * 0.052),
                             margin_v=int(SHORT_H * 0.22)), encoding="utf-8")

    # Each clip is normalised to the output frame before anything is joined, so
    # concat never has to reconcile two geometries and no scale survives past
    # the join.
    parts, inputs = [], []
    for i, c in enumerate(clips):
        inputs += ["-i", str(c)]
        parts.append(
            f"[{i}:v]scale={SHORT_W}:{SHORT_H}:force_original_aspect_ratio=increase,"
            f"crop={SHORT_W}:{SHORT_H},setsar=1,fps=30[c{i}]")
    joined = "".join(f"[c{i}]" for i in range(len(clips)))
    chain = (";".join(parts) + ";" +
             f"{joined}concat=n={len(clips)}:v=1:a=0[cut];"
             # Subtitles last, at native resolution, nothing after it.
             f"[cut]subtitles='{ass.as_posix()}'[v]")

    _ffmpeg([*inputs, "-ss", f"{start_s:.3f}", "-i", str(audio),
             "-filter_complex", chain,
             "-map", "[v]", "-map", f"{len(clips)}:a",
             "-c:v", "libx264", "-preset", "slow", "-crf", "18",
             # A high maxrate matters more than the CRF here: thin white strokes
             # on a moving background are the first thing a platform's re-encode
             # destroys, and giving it a clean high-bitrate source is the only
             # control this end has over that.
             "-maxrate", "12M", "-bufsize", "24M",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-t", f"{duration_s:.2f}", "-movflags", "+faststart", str(dest)],
            f"hook short for {dest.name}")
    ass.unlink(missing_ok=True)
    return dest


def beat_grid(bpm: int, span_s: float, *, every: int = 4) -> list[float]:
    """Cut points, every `every` beats. Exact because the BPM was specified.

    The Director asked for this tempo and the Prompt Compiler sent it, so the
    grid is a fact about the brief rather than an estimate from a waveform.
    Cutting every four beats is a bar at common time, which is where a cut feels
    intentional rather than early.
    """
    if bpm <= 0:
        return []
    step = (60.0 / bpm) * every
    out, t = [], step
    while t < span_s:
        out.append(round(t, 3))
        t += step
    return out
