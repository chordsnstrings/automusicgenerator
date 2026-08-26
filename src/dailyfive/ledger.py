"""Per-run record of every brain call.

Two reasons this exists rather than a log line.

Visibility: "twelve agents" is a claim until you can see which ones ran today,
on which brain, how long they took and which ones failed. The console reads
this table.

Attribution: when output quality changes, the first question is whether the
brain changed. Recording the provider and model against every call makes that
answerable instead of a guess.

Writes are best-effort. A ledger failure must never take down a run — losing an
audit row is a nuisance, losing the day's songs is not.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from .db import session_scope
from .models import AgentCall

log = logging.getLogger(__name__)

# Set by the pipeline for the duration of a run. A ContextVar rather than a
# global so a future concurrent run cannot attribute calls to the wrong day.
_current_run: ContextVar[int | None] = ContextVar("dailyfive_run_id", default=None)


def bind_run(run_id: int | None) -> None:
    _current_run.set(run_id)


def current_run() -> int | None:
    return _current_run.get()


def record_call(role: str, brain, label: str, *, ok: bool, ms: int,
                chars_in: int, chars_out: int, error: str | None = None) -> None:
    try:
        with session_scope() as s:
            s.add(AgentCall(
                run_id=_current_run.get(), role=role, label=label[:80],
                provider=getattr(brain, "provider", None),
                model=getattr(brain, "model", None),
                ok=ok, ms=ms, chars_in=chars_in, chars_out=chars_out,
                error=(error or None) and error[:2000]))
    except Exception:
        log.debug("ledger write failed for role %s", role, exc_info=True)


def for_run(run_id: int) -> list[dict]:
    from sqlalchemy import select
    with session_scope() as s:
        rows = s.execute(select(AgentCall).where(AgentCall.run_id == run_id)
                         .order_by(AgentCall.id)).scalars().all()
        return [_row(c) for c in rows]


def recent(limit: int = 200) -> list[dict]:
    from sqlalchemy import select
    with session_scope() as s:
        rows = s.execute(select(AgentCall).order_by(AgentCall.id.desc())
                         .limit(limit)).scalars().all()
        return [_row(c) for c in rows]


def role_summary(days: int = 30) -> dict[str, dict]:
    """Per-role totals. What the console shows on the agents page."""
    from datetime import timedelta
    from sqlalchemy import case, func, select
    from .models import utcnow
    cutoff = utcnow() - timedelta(days=days)
    with session_scope() as s:
        rows = s.execute(
            select(AgentCall.role, AgentCall.provider, AgentCall.model,
                   func.count(AgentCall.id), func.sum(AgentCall.ms),
                   func.sum(AgentCall.chars_out),
                   func.sum(case((AgentCall.ok.is_(False), 1), else_=0)))
            .where(AgentCall.created_at >= cutoff)
            .group_by(AgentCall.role, AgentCall.provider, AgentCall.model)).all()
    out: dict[str, dict] = {}
    for role, provider, model, n, ms, chars, fails in rows:
        entry = out.setdefault(role, {"calls": 0, "ms": 0, "chars_out": 0,
                                      "failures": 0, "brains": []})
        entry["calls"] += n or 0
        entry["ms"] += ms or 0
        entry["chars_out"] += chars or 0
        entry["failures"] += fails or 0
        if provider:
            entry["brains"].append(f"{provider}:{model}")
    return out


def _row(c: AgentCall) -> dict:
    return {"id": c.id, "run_id": c.run_id, "role": c.role, "label": c.label,
            "provider": c.provider, "model": c.model, "ok": c.ok, "ms": c.ms,
            "chars_in": c.chars_in, "chars_out": c.chars_out, "error": c.error,
            "created_at": c.created_at}
