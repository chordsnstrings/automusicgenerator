"""Render a lyric video by driving a browser and capturing what it composites.

The browser is here for one reason: it draws text properly. Every other path
rasterises glyphs once and then pushes them through a scale or a subtitle
filter; this one hands the encoder pixels the browser already laid out at output
resolution, with real kerning, real font hinting and whatever CSS the treatment
asks for. Kinetic typography — words arriving one at a time, on the beat — is a
handful of CSS lines here and is not expressible in a subtitle track at all.

Capture is a CDP screencast rather than a screenshot per frame. A screenshot is
a request and a reply, and at 1920x1080 that round trip costs about 137ms even
encoding JPEG, which is eighteen minutes of CPU for a five-minute song. The
screencast pushes frames as the compositor produces them and finishes in about
the length of the song.

The thing that makes real-time capture safe here is that the page is driven by
`seek(elapsed)`, not by CSS playback. The animation has no clock of its own, so
a frame that arrives late still shows the correct moment, and a frame that is
dropped costs one sample rather than shifting everything after it. Every
captured frame is stamped with the offset it was asked for and assembled through
the concat demuxer with explicit durations, so the output is exactly as long as
the audio no matter what the frame rate did in the middle.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .video import Line, _ffmpeg

log = logging.getLogger(__name__)

CHROMIUM = "/opt/pw-browsers/chromium"

# Below this many frames the capture did not work and the caller should fall
# back rather than ship four seconds of stills as a five-minute video.
MIN_FPS = 4.0


class BrowserUnavailable(RuntimeError):
    """No Chromium, or it would not start. The ffmpeg path still works."""


@dataclass
class Capture:
    frames: int
    seconds: float
    fps: float


def _executable() -> str:
    if Path(CHROMIUM).exists():
        return CHROMIUM
    found = shutil.which("chromium") or shutil.which("chromium-browser")
    if not found:
        raise BrowserUnavailable("no chromium found")
    return found


def capture(page_html: Path, out_dir: Path, *, duration_s: float,
            lines: list[Line], cover: Path | None = None,
            width: int = 1920, height: int = 1080,
            payload: dict | None = None) -> Capture:
    """Drive the page for `duration_s` and write every composited frame.

    Returns what was actually captured rather than what was asked for, because
    the frame rate is whatever the machine managed and the caller needs to know
    before it builds a video out of it.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:                       # pragma: no cover
        raise BrowserUnavailable(f"playwright not installed: {exc}") from exc

    exe = _executable()
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("f*.jpg"):
        stale.unlink()

    art = ""
    if cover and cover.is_file():
        art = ("data:image/jpeg;base64,"
               + base64.b64encode(cover.read_bytes()).decode())

    data = {
        "lines": [{"start": round(l.start, 3), "end": round(l.end, 3),
                   "text": l.text, "section": l.section} for l in lines],
        "cover": art,
        **(payload or {}),
    }

    stamps: list[tuple[str, float]] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=exe,
                args=["--hide-scrollbars", "--mute-audio",
                      "--disable-gpu-vsync", "--disable-frame-rate-limit"])
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(page_html.resolve().as_uri())
            page.evaluate(
                "d => { window.LINES = d.lines; window.COVER = d.cover; "
                "window.DATA = d; if (window.setup) window.setup(d); }", data)

            cdp = page.context.new_cdp_session(page)
            n = 0

            def on_frame(ev):
                nonlocal n
                # Stamped with the offset the page was showing, not with when
                # the frame arrived — that is what keeps a late frame correct.
                path = out_dir / f"f{n:06d}.jpg"
                path.write_bytes(base64.b64decode(ev["data"]))
                stamps.append((path.name, _clock[0]))
                n += 1
                try:
                    cdp.send("Page.screencastFrameAck",
                             {"sessionId": ev["sessionId"]})
                except Exception:
                    pass

            _clock = [0.0]
            cdp.on("Page.screencastFrame", on_frame)
            cdp.send("Page.startScreencast",
                     {"format": "jpeg", "quality": 88, "everyNthFrame": 1})

            t0 = time.time()
            while True:
                _clock[0] = time.time() - t0
                if _clock[0] >= duration_s:
                    break
                page.evaluate(f"seek({_clock[0]:.3f})")
            cdp.send("Page.stopScreencast")
            browser.close()
    except BrowserUnavailable:
        raise
    except Exception as exc:
        raise BrowserUnavailable(f"capture failed: {exc}") from exc

    if not stamps:
        raise BrowserUnavailable("the screencast produced no frames")

    # Explicit per-frame durations rather than a nominal frame rate. The gaps
    # are uneven by construction and a fixed -r would stretch or squash the
    # whole thing against the audio.
    listing = []
    for i, (name, at) in enumerate(stamps):
        nxt = stamps[i + 1][1] if i + 1 < len(stamps) else duration_s
        listing.append(f"file '{name}'\nduration {max(0.001, nxt - at):.4f}")
    listing.append(f"file '{stamps[-1][0]}'")      # concat needs the last twice
    (out_dir / "frames.txt").write_text("\n".join(listing) + "\n")

    span = stamps[-1][1] or duration_s
    return Capture(frames=len(stamps), seconds=span, fps=len(stamps) / max(span, 0.001))


def assemble(frames_dir: Path, audio: Path, dest: Path, *,
             duration_s: float, fps: int = 25) -> Path:
    """Frames plus the master into a deliverable file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(frames_dir / "frames.txt"),
             "-i", str(audio),
             # The concat timings are the truth; -r resamples them onto a
             # constant grid so players that dislike variable frame rate behave.
             "-vf", f"fps={fps},format=yuv420p",
             "-c:v", "libx264", "-preset", "medium", "-crf", "19",
             "-c:a", "aac", "-b:a", "192k", "-shortest",
             "-t", f"{duration_s:.2f}", "-movflags", "+faststart", str(dest)],
            f"assembling {dest.name}")
    return dest


def render(page_html: Path, audio: Path, dest: Path, *, lines: list[Line],
           duration_s: float, cover: Path | None = None,
           work: Path | None = None, payload: dict | None = None,
           width: int = 1920, height: int = 1080) -> Capture:
    """Capture and assemble. Raises BrowserUnavailable so a caller can fall back.

    Deliberately not swallowing that: a lyric video is worth having in a lesser
    form, and the ffmpeg path in video.py produces one without a browser. What
    is not worth having is a run that fails because a rendering nicety could not
    be met.
    """
    work = work or dest.parent / f".frames-{dest.stem}"
    cap = capture(page_html, work, duration_s=duration_s, lines=lines,
                  cover=cover, width=width, height=height, payload=payload)
    if cap.fps < MIN_FPS:
        raise BrowserUnavailable(
            f"only {cap.fps:.1f} fps captured — too sparse to ship")
    assemble(work, audio, dest, duration_s=duration_s)
    for f in work.glob("f*.jpg"):
        f.unlink()
    (work / "frames.txt").unlink(missing_ok=True)
    work.rmdir()
    log.info("rendered %s from %d browser frames (%.1f fps)",
             dest.name, cap.frames, cap.fps)
    return cap
