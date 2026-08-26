"""Engine and session handling. SQLite for day one, Postgres once webhooks land."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings
from .models import Base

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def engine() -> Engine:
    global _engine, _Session
    if _engine is None:
        url = settings().database_url
        kwargs: dict = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            # A webhook thread and the run thread both touch this database.
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            kwargs.pop("pool_pre_ping")
            _ensure_sqlite_dir(url)
            if ":memory:" in url or "mode=memory" in url:
                # An in-memory database lives inside one connection. With the
                # default pool, every session opens a fresh connection and
                # therefore a fresh empty database — tables created at startup
                # simply are not there on the next call.
                kwargs["poolclass"] = StaticPool
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                # WAL so the webhook receiver can read while the run writes.
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def _ensure_sqlite_dir(url: str) -> None:
    """Create the directory a file-based SQLite database lives in.

    Without this, a DATABASE_URL pointing at a subdirectory fails with a bare
    "unable to open database file" and a forty-line traceback, which reads like
    a permissions problem rather than a missing folder.
    """
    if ":memory:" in url or "mode=memory" in url:
        return
    path = url.split("///", 1)[-1].split("?", 1)[0]
    if not path or path == ":memory:":
        return
    parent = Path(path).expanduser().parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    Base.metadata.create_all(engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    engine()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def reset_engine() -> None:
    """Drop the cached engine. Tests use this after changing DATABASE_URL."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None
