"""The vertical short: one delivered song, one dancer, two takes, cut on the beat.

Runs after delivery rather than inside it, and that separation is the whole
shape of this module. The free video allowance is three generations a day and a
short costs two, so exactly one of the day's five songs gets one — and a video
that fails must never cost the day its music. Delivery finishes first; this runs
against a song that already exists.

Everything expensive is cached in the work directory under the clip id, so a
crash halfway through re-uses the still and any finished clip rather than
spending the allowance again. That is not an optimisation. At three generations
a day, one careless re-run is the whole day's footage, and the failure it
protects against — a process restarting mid-poll — is the one that actually
happens.

The pieces each have their own file, and this is only the wiring:

    videodirector   what the camera sees, and why shot two is not shot one
    cast            who is in it, and the terms that are never negotiable
    modelark        the reference still, in portrait
    minimax         each shot animated from that still
    video           the cut, on a beat grid taken from the brief's own BPM
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import video
from .agents import videodirector
from .cast import Performer, clip_prompt, pick, still_prompt
from .config import settings
from .errors import ProviderError
from .http import download
from .providers.minimax import MiniMaxVideoClient
from .providers.modelark import ModelArkClient

log = logging.getLogger(__name__)


@dataclass
class Short:
    """What was made, and what it was made from."""
    path: Path
    clip_id: int
    performer: str
    shots: list[dict] = field(default_factory=list)
    clips: list[Path] = field(default_factory=list)
    still: Path | None = None
    start_s: float = 0.0
    duration_s: float = 0.0
    reused: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "clip_id": self.clip_id,
            "performer": self.performer,
            "shots": self.shots,
            "start_s": round(self.start_s, 2),
            "duration_s": round(self.duration_s, 2),
            "clips": [p.name for p in self.clips],
            "reused": self.reused,
            "file": self.path.name,
        }


def make(*, clip_id: int, brief: dict, audio: Path, lrc: Path | None,
         run_date: date, dest: Path, work: Path | None = None,
         shots: int | None = None, seconds: int | None = None,
         resolution: str | None = None, force: bool = False) -> Short:
    """Build one short. Everything it needs is passed in; nothing is queried here.

    Taking the brief and the audio as arguments rather than reading them back
    out of the database is what makes this testable and what lets the same
    function serve the pipeline, the CLI and a one-off rebuild of a song from
    three weeks ago. The caller owns knowing which song.
    """
    cfg = settings()
    n = shots or cfg.video_clips
    secs = seconds or cfg.video_clip_seconds
    res = resolution or cfg.video_resolution

    work = work or (cfg.work_dir / "shorts" / str(clip_id))
    work.mkdir(parents=True, exist_ok=True)
    reused: list[str] = []

    performer = pick(run_date=run_date.isoformat(), clip_id=clip_id)
    log.info("short for clip %d: %s, %d x %ds at %s",
             clip_id, performer.key, n, secs, res)

    shot_list = _shot_list(work, brief, performer, n=n, secs=secs, force=force,
                           reused=reused)
    still = _still(work, performer, force=force, reused=reused)
    clips = _clips(work, performer, shot_list, still,
                   secs=secs, res=res, bpm=brief.get("bpm"),
                   force=force, reused=reused)

    # Where in the song. The hook window is asked for the exact length the
    # footage covers, so the cut never has to hold a frame or drop one.
    span = float(n * secs)
    lines = video.parse_lrc(lrc.read_text(encoding="utf-8")) if lrc and lrc.is_file() else []
    start, _ = video.hook_window(lines, want_s=span)

    video.hook_short(clips, audio, dest, start_s=start, duration_s=span,
                     bpm=brief.get("bpm"))
    return Short(path=dest, clip_id=clip_id, performer=performer.key,
                 shots=shot_list, clips=clips, still=still,
                 start_s=start, duration_s=span, reused=reused)


# ── the expensive steps, each resumable ──────────────────────────────────────
def _shot_list(work: Path, brief: dict, performer: Performer, *,
               n: int, secs: int, force: bool, reused: list[str]) -> list[dict]:
    cached = work / "shots.json"
    if cached.is_file() and not force:
        try:
            shots = json.loads(cached.read_text())
            if isinstance(shots, list) and len(shots) == n:
                reused.append("shots")
                return shots
        except (ValueError, OSError) as exc:
            log.warning("unreadable cached shot list, re-planning: %s", exc)

    shots = videodirector.plan(brief, performer, shots=n, seconds_each=secs,
                               bpm=brief.get("bpm"))
    cached.write_text(json.dumps(shots, indent=2), encoding="utf-8")
    return shots


def _still(work: Path, performer: Performer, *, force: bool,
           reused: list[str]) -> Path:
    dest = work / f"still-{performer.key}.jpg"
    if dest.is_file() and dest.stat().st_size > 1024 and not force:
        reused.append("still")
        return dest
    url = ModelArkClient().still(still_prompt(performer))
    download(url, dest, provider="modelark")
    log.info("reference still: %s (%.0f KB)", dest.name, dest.stat().st_size / 1024)
    return dest


def _clips(work: Path, performer: Performer, shots: list[dict], still: Path, *,
           secs: int, res: str, bpm: int | None, force: bool,
           reused: list[str]) -> list[Path]:
    """Submit every shot, then wait on all of them.

    Submitted first and waited on second because these queue for minutes on the
    free allowance, and running them one after the other doubles the wall clock
    for no reason — the second job does not depend on the first.
    """
    client = MiniMaxVideoClient()
    frame = _data_uri(still)

    paths = [work / f"clip{i}.mp4" for i in range(len(shots))]
    pending: list[tuple[int, str]] = []

    for i, (shot, path) in enumerate(zip(shots, paths)):
        if path.is_file() and path.stat().st_size > 10_000 and not force:
            reused.append(f"clip{i}")
            log.info("clip %d already generated, reusing it", i)
            continue
        prompt = clip_prompt(performer, videodirector.shot_line(shot), bpm=bpm)
        pending.append((i, client.submit(prompt, first_frame=frame,
                                         duration=secs, resolution=res)))

    failures: list[str] = []
    for i, task_id in pending:
        try:
            url = client.wait(task_id)
            download(url, paths[i], provider="minimax-video")
            log.info("clip %d: %s (%.1f MB)", i, paths[i].name,
                     paths[i].stat().st_size / 1e6)
        except ProviderError as exc:
            # Recorded and carried on with. A short cut from one take is worse
            # than one cut from two and better than no short at all — and the
            # allowance is already spent either way, so failing the whole build
            # here would waste a generation that succeeded.
            failures.append(f"clip {i}: {exc}")
            log.error("clip %d failed: %s", i, exc)

    got = [p for p in paths if p.is_file() and p.stat().st_size > 10_000]
    if not got:
        why = "; ".join(failures) or "nothing was submitted"
        raise ProviderError("minimax-video", f"no clips were generated: {why}",
                            retryable=False)
    if failures:
        log.warning("short is short of footage — %d of %d takes: %s",
                    len(got), len(paths), "; ".join(failures))
    return got


def _data_uri(path: Path) -> str:
    """The still as a data URI rather than the provider's own URL.

    ModelArk hands back a link that lives about a week, and passing it through
    would make the video generation depend on a third party fetching a fourth
    party at an unpredictable moment. The bytes are already local; send those.
    """
    return ("data:image/jpeg;base64,"
            + base64.b64encode(path.read_bytes()).decode())
