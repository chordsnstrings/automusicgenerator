"""The worker loop.

App Platform Job components time out after 30 minutes, and this run
deliberately waits on someone else's render queue — the whole point of the
poller is to be patient when Suno is slow, which is exactly when a job would be
killed. So the run lives in a long-running Worker with its own clock instead.

The clock is deliberately dumb: wake every minute, and if the wall time has
crossed a scheduled slot that has not run today, run it. No cron parsing, no
drift correction, no missed-fire semantics to get wrong. A restart mid-window
re-checks and picks up, because the pipeline is idempotent per date and resumes
from the phase it recorded.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from .config import settings

log = logging.getLogger(__name__)

# UTC. Backup before the run so the copy is of a quiet database; purge well
# clear of both so it can never race a run that is still writing.
SLOTS = (
    ("purge", 3, 0),
    ("backup", 4, 30),
    ("run", 5, 10),
)


def _today_at(hour: int, minute: int) -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def run_forever(*, tick_seconds: int = 60, once: bool = False) -> None:
    from .db import init_db
    init_db()

    last_done: dict[str, date] = {}
    log.info("scheduler up — slots (UTC): %s",
             ", ".join(f"{n} {h:02d}:{m:02d}" for n, h, m in SLOTS))

    while True:
        now = datetime.now(timezone.utc)
        for name, hour, minute in SLOTS:
            due_at = _today_at(hour, minute)
            if now < due_at:
                continue
            if last_done.get(name) == now.date():
                continue
            # More than an hour late means the worker was down through the
            # window; the run is still worth doing, the purge and backup are
            # not urgent enough to fire out of order.
            if name != "run" and now - due_at > timedelta(hours=6):
                last_done[name] = now.date()
                continue
            last_done[name] = now.date()
            _fire(name)
        if once:
            return
        time.sleep(tick_seconds)


def _fire(name: str) -> None:
    log.info("scheduler firing %s", name)
    try:
        if name == "run":
            from .pipeline import run_daily
            summary = run_daily()
            log.info("run complete: %s", summary)
        elif name == "purge":
            from .retention import purge
            log.info("purge: %s", purge())
        elif name == "backup":
            from .backup import dump
            path = dump()
            log.info("backup: %s (%.1f MB)", path, path.stat().st_size / 1e6)
    except Exception:
        # A failed slot must not take the worker down — tomorrow's run is
        # still wanted, and App Platform restarting the container in a loop
        # would just fail faster.
        log.exception("scheduler slot %s failed", name)
