"""Retention: delete what has outlived its window.

The window is stamped on every row when it is written, so this job reads
``expires_at`` and nothing else. That matters more than it sounds: a purge that
recomputes eligibility from run dates or file names is a second place the rule
lives, and the two drift. One column, one comparison, one job.

Deliberately conservative in one direction: metadata is never deleted. The
clip rows, QC numbers, producer scores, rejection reasons and your ratings are
the learning record, and they are small. Only the bytes expire.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select

from .config import settings
from .db import session_scope
from .models import StoredFile, utcnow

log = logging.getLogger(__name__)


def due(as_of=None) -> int:
    """How many stored files are past their expiry right now."""
    now = as_of or utcnow()
    with session_scope() as s:
        return s.execute(
            select(func.count(StoredFile.id))
            .where(StoredFile.expires_at.isnot(None),
                   StoredFile.expires_at <= now)).scalar() or 0


def purge(*, dry_run: bool = False, batch: int = 200) -> dict:
    """Delete expired bytes. Returns what went, so the run log can say.

    Batched because a month of WAVs is gigabytes, and one enormous DELETE on a
    managed cluster is a long lock and a large WAL spike for no benefit.
    """
    now = utcnow()
    removed = 0
    freed = 0
    by_kind: dict[str, int] = {}

    while True:
        with session_scope() as s:
            rows = s.execute(
                select(StoredFile.id, StoredFile.key, StoredFile.kind,
                       StoredFile.size_bytes)
                .where(StoredFile.expires_at.isnot(None),
                       StoredFile.expires_at <= now)
                .order_by(StoredFile.expires_at)
                .limit(batch)).all()
            if not rows:
                break
            if dry_run:
                for _id, key, kind, size in rows:
                    removed += 1
                    freed += size or 0
                    by_kind[kind] = by_kind.get(kind, 0) + 1
                break

            for _id, key, kind, size in rows:
                s.get(StoredFile, _id) and s.delete(s.get(StoredFile, _id))
                removed += 1
                freed += size or 0
                by_kind[kind] = by_kind.get(kind, 0) + 1

    result = {"removed": removed, "freed_mb": round(freed / 1e6, 1),
              "by_kind": by_kind, "dry_run": dry_run,
              "retention_days": settings().retention_days}
    if removed:
        log.info("retention: %s", result)
    return result


def usage() -> dict:
    """What the store is holding, and when the next thing expires."""
    with session_scope() as s:
        total, count = s.execute(
            select(func.coalesce(func.sum(StoredFile.size_bytes), 0),
                   func.count(StoredFile.id))).one()
        rows = s.execute(
            select(StoredFile.kind, func.count(StoredFile.id),
                   func.coalesce(func.sum(StoredFile.size_bytes), 0))
            .group_by(StoredFile.kind)).all()
        soonest = s.execute(
            select(func.min(StoredFile.expires_at))
            .where(StoredFile.expires_at.isnot(None))).scalar()
        oldest = s.execute(select(func.min(StoredFile.created_at))).scalar()

    cfg = settings()
    return {
        "files": count,
        "total_gb": round((total or 0) / 1e9, 2),
        "by_kind": {k: {"files": n, "gb": round((b or 0) / 1e9, 2)}
                    for k, n, b in rows},
        "retention_days": cfg.retention_days,
        "next_expiry": soonest.isoformat() if soonest else None,
        "oldest_file": oldest.isoformat() if oldest else None,
        "due_now": due(),
        "projected_steady_gb": _projected(cfg),
    }


def _projected(cfg) -> float:
    """What the store settles at once the window is full.

    Worth showing rather than discovering: the number climbs for a month and
    then stops, and knowing where it stops is the difference between a planned
    plan size and an outage.
    """
    per_song_mb = 55          # a ~4 minute WAV plus its 320kbps MP3
    return round(cfg.total_slots * cfg.retention_days * per_song_mb / 1000, 1)
