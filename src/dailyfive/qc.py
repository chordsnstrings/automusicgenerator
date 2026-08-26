"""QC Engineer — measurement, not judgment. No language model touches this file.

Suno fails in ways nothing can hear by reading a prompt: a track that ends
mid-bar, forty seconds of near-silence with a tail of noise, a hard clip, a
"three minute" song that came back at forty-seven seconds. All of those are
numbers, and numbers are what this module produces.

Everything runs through ffmpeg, which the droplet needs anyway for MP3
encoding — ``ebur128`` for loudness and true peak, ``astats`` for clipping and
DC offset, ``silencedetect`` for dead air. No signal-processing library, no
model, and therefore nothing that can hallucinate that the audio is fine.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Thresholds. Deliberately loose — this gate exists to catch broken files, not
# to impose taste. Anything that passes here still has to win on merit.
THRESHOLDS = {
    "min_duration_full_s": 90.0,
    "min_duration_short_s": 20.0,
    "max_duration_delta_ratio": 0.45,   # asked 45s, got under 25s or over 65s
    "max_true_peak_db": 0.0,            # anything at or above 0 dBFS is clipping
    "max_clip_samples": 64,
    "min_lufs": -30.0,                  # quieter than this is a failed render
    "max_lufs": -4.0,                   # louder is a crushed one
    "max_silence_ratio": 0.35,
    "max_lead_silence_s": 6.0,
    "max_tail_silence_s": 12.0,
    "min_file_bytes": 64_000,
}


class FFmpegMissing(RuntimeError):
    """ffmpeg is not on PATH. Fatal for a real run, skippable in tests."""


@dataclass
class QCMetrics:
    duration_s: float | None = None
    lufs_i: float | None = None
    true_peak_db: float | None = None
    peak_db: float | None = None
    dc_offset: float | None = None
    clip_samples: int = 0
    silence_ratio: float = 0.0
    lead_silence_s: float = 0.0
    tail_silence_s: float = 0.0
    file_bytes: int = 0
    sample_rate: int | None = None
    channels: int | None = None
    measured: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def measure(path: Path | str) -> QCMetrics:
    """Everything measurable about one audio file, in two subprocess calls."""
    path = Path(path)
    m = QCMetrics()
    if not path.is_file():
        m.notes.append("file missing")
        return m
    m.file_bytes = path.stat().st_size

    if not have_ffmpeg():
        raise FFmpegMissing(
            "ffmpeg and ffprobe are required for QC. Install with "
            "`apt-get install -y ffmpeg` on the droplet.")

    _probe(path, m)
    _analyse(path, m)
    m.measured = m.duration_s is not None
    return m


def _probe(path: Path, m: QCMetrics) -> None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "format=duration:stream=sample_rate,channels",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120, check=False).stdout
        data = json.loads(out or "{}")
        dur = (data.get("format") or {}).get("duration")
        m.duration_s = round(float(dur), 3) if dur else None
        streams = data.get("streams") or []
        if streams:
            m.sample_rate = _int(streams[0].get("sample_rate"))
            m.channels = _int(streams[0].get("channels"))
    except Exception as exc:
        m.notes.append(f"ffprobe failed: {exc}")


_RE_I = re.compile(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", re.M)
_RE_TP = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", re.M)
_RE_PEAK = re.compile(r"Peak level dB:\s*(-?[\d.]+|-inf)", re.M)
_RE_DC = re.compile(r"DC offset:\s*(-?[\d.]+)", re.M)
_RE_CLIP = re.compile(r"Number of clipped samples:\s*(\d+)", re.M)
_RE_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)", re.M)
_RE_SIL_END = re.compile(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", re.M)


def _analyse(path: Path, m: QCMetrics) -> None:
    """One ffmpeg pass carrying all three filters. stderr holds the results."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostats", "-hide_banner", "-i", str(path),
             "-af", "ebur128=peak=true,astats=metadata=1:reset=0,"
                    "silencedetect=noise=-50dB:d=0.4",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=600, check=False)
    except Exception as exc:
        m.notes.append(f"ffmpeg analysis failed: {exc}")
        return

    err = proc.stderr or ""
    m.lufs_i = _f(_last(_RE_I, err))
    m.true_peak_db = _f(_last(_RE_TP, err))
    m.peak_db = _f(_last(_RE_PEAK, err))
    m.dc_offset = _f(_last(_RE_DC, err))
    clip = _last(_RE_CLIP, err)
    m.clip_samples = int(clip) if clip and clip.isdigit() else 0

    # ebur128 reports true peak per channel; astats' peak is the fallback.
    if m.true_peak_db is None and m.peak_db is not None:
        m.true_peak_db = m.peak_db
        m.notes.append("true peak unavailable, using astats peak level")

    _silence(err, m)


def _silence(err: str, m: QCMetrics) -> None:
    starts = [float(x) for x in _RE_SIL_START.findall(err)]
    spans = [(float(end), float(dur)) for end, dur in _RE_SIL_END.findall(err)]
    total = sum(d for _, d in spans)

    # A silence that opened and never closed runs to the end of the file.
    if len(starts) > len(spans) and m.duration_s:
        total += max(0.0, m.duration_s - starts[-1])
        m.tail_silence_s = round(max(0.0, m.duration_s - starts[-1]), 3)
    elif spans and m.duration_s:
        last_end = spans[-1][0]
        if m.duration_s - last_end < 0.25 and starts:
            m.tail_silence_s = round(spans[-1][1], 3)

    if starts and starts[0] <= 0.05 and spans:
        m.lead_silence_s = round(spans[0][1], 3)

    if m.duration_s and m.duration_s > 0:
        m.silence_ratio = round(min(1.0, total / m.duration_s), 4)


def verdict(m: QCMetrics, *, slot_type: str = "full",
            requested_duration_s: float | None = None) -> tuple[str, list[str]]:
    """('pass'|'fail', reasons). Every reason names the number that failed."""
    t = THRESHOLDS
    reasons: list[str] = []

    if not m.measured:
        return "fail", ["could not be measured"] + m.notes
    if m.file_bytes < t["min_file_bytes"]:
        reasons.append(f"file is {m.file_bytes} bytes, under {t['min_file_bytes']}")

    dur = m.duration_s or 0.0
    floor = t["min_duration_short_s"] if slot_type == "short" else t["min_duration_full_s"]
    if dur < floor:
        reasons.append(f"duration {dur:.1f}s under the {floor:.0f}s floor for a {slot_type} cut")

    if requested_duration_s:
        delta = abs(dur - requested_duration_s) / requested_duration_s
        if delta > t["max_duration_delta_ratio"]:
            reasons.append(
                f"duration {dur:.1f}s is {delta * 100:.0f}% off the "
                f"{requested_duration_s:.0f}s requested")

    if m.true_peak_db is not None and m.true_peak_db >= t["max_true_peak_db"]:
        reasons.append(f"true peak {m.true_peak_db:+.2f} dBFS is clipping")
    if m.clip_samples > t["max_clip_samples"]:
        reasons.append(f"{m.clip_samples} clipped samples")

    if m.lufs_i is not None:
        if m.lufs_i < t["min_lufs"]:
            reasons.append(f"integrated loudness {m.lufs_i:.1f} LUFS — render is near-silent")
        elif m.lufs_i > t["max_lufs"]:
            reasons.append(f"integrated loudness {m.lufs_i:.1f} LUFS — render is crushed")

    if m.silence_ratio > t["max_silence_ratio"]:
        reasons.append(f"{m.silence_ratio * 100:.0f}% of the track is silence")
    if m.lead_silence_s > t["max_lead_silence_s"]:
        reasons.append(f"{m.lead_silence_s:.1f}s of silence before the music starts")
    if m.tail_silence_s > t["max_tail_silence_s"]:
        reasons.append(f"{m.tail_silence_s:.1f}s of dead air at the end")

    if m.dc_offset is not None and abs(m.dc_offset) > 0.02:
        reasons.append(f"DC offset {m.dc_offset:+.3f}")

    return ("fail", reasons) if reasons else ("pass", [])


def trim_points(m: QCMetrics) -> tuple[float, float | None]:
    """Where to cut for the delivery master.

    Conservative on purpose: a little air at the head is normal, and trimming
    into the first transient is worse than leaving half a second of room.
    """
    start = max(0.0, m.lead_silence_s - 0.15) if m.lead_silence_s > 0.5 else 0.0
    end = None
    if m.duration_s and m.tail_silence_s > 1.0:
        end = max(start + 1.0, m.duration_s - m.tail_silence_s + 0.35)
    return round(start, 3), (round(end, 3) if end else None)


def _last(rx: re.Pattern, text: str) -> str | None:
    found = rx.findall(text)
    if not found:
        return None
    val = found[-1]
    return val[0] if isinstance(val, tuple) else val


def _f(v) -> float | None:
    if v is None or v == "-inf":
        return None
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
