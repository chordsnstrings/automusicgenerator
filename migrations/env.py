"""Alembic environment.

Reads DATABASE_URL rather than alembic.ini so one set of migrations serves the
SQLite you start on and the Postgres you move to. SQLite gets batch mode, which
is what makes ALTER TABLE work there at all.

On Postgres the whole upgrade runs under one advisory lock, because two
containers run it at the same moment on every deploy — see MIGRATION_LOCK_KEY.
"""

from __future__ import annotations

import logging
import sys
import time
import zlib
from contextlib import contextmanager
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dailyfive.config import settings          # noqa: E402
from dailyfive.models import Base              # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata

log = logging.getLogger("alembic.env")

# Both the web service and the scheduler call `alembic upgrade head` at
# container start (cli.py serve and cli.py scheduler, via init_db), so on every
# deploy two processes read the version table and decide what to apply within
# the same second. Nothing has serialised them until now: the first three
# migrations never exercised the race because the database was either empty or
# single-process, and the retry loop around init_db turned the collision into
# one alarming log line rather than a failure. It is luck, not design — the
# actual failure mode is both containers seeing the same current revision,
# both running ALTER TABLE, and the loser dying on "column already exists"
# with the version table possibly already stamped by the winner.
#
# A session-level advisory lock, not pg_advisory_xact_lock: Alembic wraps the
# whole upgrade in one transaction by default, but transaction_per_migration
# would break that assumption silently, and a session lock covers the version
# table read as well as the DDL. It is released explicitly below and, if this
# process dies, by Postgres when the connection drops — which is the property
# that makes it safe to hold across a container that gets killed mid-deploy.
#
# The key is derived rather than invented so a reader can check it:
# zlib.crc32(b"dailyfive:migrations"). Any other application sharing the
# cluster would have to pick the same string to collide.
MIGRATION_LOCK_KEY = zlib.crc32(b"dailyfive:migrations")
MIGRATION_LOCK_WAIT_S = 120.0
MIGRATION_LOCK_POLL_S = 1.0


def _is_sqlite() -> bool:
    return settings().database_url.startswith("sqlite")


@contextmanager
def _migration_lock(connection):
    """Hold the upgrade lock, or pass straight through on SQLite.

    SQLite has no advisory locks and does not need them — it is the test
    dialect, one process per database, and the file lock already serialises
    writers. Keyed off the live dialect rather than the URL so a connection is
    never asked for a function it does not have.

    Polled rather than blocking. `pg_advisory_lock` would wait forever, and a
    deploy that hangs with no output is harder to diagnose than one that fails
    after two minutes naming the reason.
    """
    if connection.dialect.name != "postgresql":
        yield
        return

    deadline = time.monotonic() + MIGRATION_LOCK_WAIT_S
    waited = False
    while True:
        got = connection.scalar(text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"),
                                {"key": MIGRATION_LOCK_KEY})
        connection.commit()          # end the autobegun transaction; the lock is
        if got:                      # session-scoped and survives the commit
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"another process has held the migration lock "
                f"({MIGRATION_LOCK_KEY}) for over {MIGRATION_LOCK_WAIT_S:.0f}s")
        if not waited:
            log.info("waiting for the migration lock — another container is upgrading")
            waited = True
        time.sleep(MIGRATION_LOCK_POLL_S)

    try:
        yield
    finally:
        # Best effort. A failed migration leaves the transaction aborted, so
        # the unlock itself would raise and mask the error that actually
        # matters; closing the connection releases a session lock anyway.
        try:
            connection.rollback()
            connection.scalar(text("SELECT pg_advisory_unlock(CAST(:key AS bigint))"),
                              {"key": MIGRATION_LOCK_KEY})
            connection.commit()
        except Exception:
            log.warning("could not release the migration lock; "
                        "closing the connection will drop it", exc_info=True)


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"},
                      render_as_batch=_is_sqlite())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        with _migration_lock(connection):
            context.configure(connection=connection, target_metadata=target_metadata,
                              render_as_batch=_is_sqlite(),
                              compare_type=True, compare_server_default=True)
            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
