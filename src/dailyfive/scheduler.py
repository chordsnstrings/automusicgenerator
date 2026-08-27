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
#
# retype follows purge because it is the same kind of housekeeping and there is
# less of the table left to walk once expired rows are gone. It runs daily rather
# than once by hand because nothing on App Platform runs the CLI — there is no
# jobs key in the spec — and because a one-shot repair cannot catch a row written
# by an old container mid rolling deploy or restored from a pre-fix backup. On a
# clean table it matches nothing and issues no UPDATE, so the standing cost is
# one query a day.
#
# remeta is there for the same reasons and not one of them is that it will keep
# finding work: new manifests are written correct. It stays daily because a
# backup taken before the fix reintroduces the false ones exactly as it
# reintroduces the wrong content types, and a repair that only ever ran once is
# a repair nobody can rerun.
# short and metrics both follow the run rather than joining it. The short costs
# two of three daily video generations and takes ten minutes of polling, and a
# provider being out of allowance must never be able to cost the day its music —
# so it is a separate slot, after delivery, on a song that already exists.
# metrics reads yesterday's counts and touches nothing else; it is last because
# it is the only slot whose value goes up the later it runs.
SLOTS = (
    ("purge", 3, 0),
    ("retype", 3, 5),
    ("remeta", 3, 10),
    ("backup", 4, 30),
    ("run", 5, 10),
    ("short", 6, 30),
    ("metrics", 7, 0),
)


def _today_at(hour: int, minute: int) -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def run_forever(*, tick_seconds: int = 60, once: bool = False,
                dry_run: bool = False) -> None:
    """Wake on a timer and fire whatever slot is due.

    ``dry_run`` reports what *would* fire and touches nothing. That exists
    because ``--once`` on a machine where the day's slots have already passed
    will start a real run and spend real credits — a sharp edge on a command
    someone would reasonably reach for just to check the configuration.
    """
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
            if dry_run:
                log.info("would fire %s (due %s)", name, due_at.strftime("%H:%M UTC"))
                print(f"  would fire {name} (due {due_at.strftime('%H:%M UTC')})")
                continue
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
        elif name == "retype":
            from .storage import retype_stored_files
            log.info("retype: %s", retype_stored_files())
        elif name == "remeta":
            from .storage import remeta_stored_files
            log.info("remeta: %s", remeta_stored_files())
        elif name == "backup":
            from .backup import dump
            path = dump()
            log.info("backup: %s (%.1f MB)", path, path.stat().st_size / 1e6)
        elif name == "short":
            from .shorts import build_for_day
            log.info("short: %s", build_for_day().as_dict())
        elif name == "metrics":
            from .publish import refresh
            log.info("metrics: %s", refresh())
    except Exception:
        # A failed slot must not take the worker down — tomorrow's run is
        # still wanted, and App Platform restarting the container in a loop
        # would just fail faster.
        log.exception("scheduler slot %s failed", name)
