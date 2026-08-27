"""The migration lock, which can never run under the test dialect.

Two containers — the web service and the scheduler — both call
``alembic upgrade head`` at start (cli.py serve and cli.py scheduler, via
init_db), so every deploy runs two upgrades within the same second. Nothing
serialised them before this: the first three migrations never exercised the
race because the database was empty or single-process, and the retry loop
around init_db turned the collision into one alarming log line rather than a
failure. Production is Postgres and the suite is SQLite, so the lock itself is
tested here against a recording stand-in rather than a real cluster.
"""

import contextlib
import pathlib

import pytest

ENV_PATH = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "env.py"


class _FakeAlembicConfig:
    config_file_name = None

    def set_main_option(self, *a, **k):
        pass

    def get_main_option(self, *a, **k):
        return "sqlite://"

    def get_section(self, *a, **k):
        return {}


@pytest.fixture
def env(monkeypatch):
    """Load migrations/env.py without an Alembic run around it.

    The module runs migrations on import, which is exactly right in production
    and useless here, so the context is stubbed into offline mode and the
    module body executed into a private namespace. Loading the real file rather
    than copying the helper is the point: a lock that drifts out of env.py is
    a lock that is not held.
    """
    import alembic.context as ctx
    monkeypatch.setattr(ctx, "config", _FakeAlembicConfig(), raising=False)
    monkeypatch.setattr(ctx, "is_offline_mode", lambda: True, raising=False)
    monkeypatch.setattr(ctx, "configure", lambda **k: None, raising=False)
    monkeypatch.setattr(ctx, "begin_transaction",
                        lambda: contextlib.nullcontext(), raising=False)
    monkeypatch.setattr(ctx, "run_migrations", lambda: None, raising=False)

    namespace = {"__file__": str(ENV_PATH), "__name__": "dailyfive_env_under_test"}
    exec(compile(ENV_PATH.read_text(), str(ENV_PATH), "exec"), namespace)
    return namespace


class _Dialect:
    def __init__(self, name):
        self.name = name


class _Connection:
    """Records what the lock asks the database to do. ``held`` counts how many
    try-lock attempts fail before one succeeds."""

    def __init__(self, dialect="postgresql", held=0):
        self.dialect = _Dialect(dialect)
        self.held = held
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def scalar(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, dict(params or {})))
        if "pg_try_advisory_lock" in sql:
            if self.held > 0:
                self.held -= 1
                return False
            return True
        return True

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _kinds(conn):
    return [s.split("(")[0].removeprefix("SELECT ").strip()
            for s, _p in conn.statements]


def test_sqlite_never_sees_an_advisory_lock(env):
    """SQLite has no advisory locks and does not need them — one process per
    database, and the file lock already serialises writers."""
    conn = _Connection(dialect="sqlite")
    with env["_migration_lock"](conn):
        pass
    assert conn.statements == []
    assert conn.commits == 0


def test_postgres_takes_and_releases_one_fixed_key(env):
    conn = _Connection()
    with env["_migration_lock"](conn):
        assert _kinds(conn) == ["pg_try_advisory_lock"]
    assert _kinds(conn) == ["pg_try_advisory_lock", "pg_advisory_unlock"]
    keys = {params["key"] for _s, params in conn.statements}
    assert keys == {env["MIGRATION_LOCK_KEY"]}


def test_the_key_is_derived_so_a_reader_can_check_it(env):
    import zlib
    assert env["MIGRATION_LOCK_KEY"] == zlib.crc32(b"dailyfive:migrations")


def test_the_lock_is_taken_before_the_upgrade_and_held_across_it(env):
    """The version-table read has to be inside the lock too, or both containers
    still decide what to apply from the same stale revision."""
    conn = _Connection()
    seen = []
    with env["_migration_lock"](conn):
        seen.append(list(_kinds(conn)))
    assert seen == [["pg_try_advisory_lock"]]


def test_a_second_container_waits_rather_than_racing(env, monkeypatch):
    monkeypatch.setattr(env["time"], "sleep", lambda _s: None)
    conn = _Connection(held=3)
    with env["_migration_lock"](conn):
        pass
    tries = [k for k in _kinds(conn) if k == "pg_try_advisory_lock"]
    assert len(tries) == 4, "the loser gave up instead of waiting its turn"


def test_a_wedged_holder_fails_the_deploy_instead_of_hanging(env, monkeypatch):
    """A deploy that hangs with no output is harder to diagnose than one that
    fails after two minutes naming the reason."""
    monkeypatch.setattr(env["time"], "sleep", lambda _s: None)
    clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 20_000.0])
    monkeypatch.setattr(env["time"], "monotonic", lambda: next(clock))
    conn = _Connection(held=999)
    with pytest.raises(TimeoutError, match=str(env["MIGRATION_LOCK_KEY"])):
        with env["_migration_lock"](conn):
            pass


def test_a_failed_migration_is_not_masked_by_the_unlock(env):
    """A failed upgrade leaves the transaction aborted, so the unlock itself
    would raise. Closing the connection drops a session lock anyway."""
    class _Broken(_Connection):
        def scalar(self, statement, params=None):
            if "pg_advisory_unlock" in str(statement):
                raise RuntimeError("current transaction is aborted")
            return super().scalar(statement, params)

    conn = _Broken()
    with pytest.raises(ValueError, match="the real failure"):
        with env["_migration_lock"](conn):
            raise ValueError("the real failure")


def test_the_lock_is_actually_wired_into_the_upgrade():
    """A helper nobody calls is not a lock."""
    source = ENV_PATH.read_text()
    online = source.split("def run_migrations_online")[1]
    assert "_migration_lock(connection)" in online
    assert online.index("_migration_lock") < online.index("context.run_migrations")


def test_migrating_does_not_switch_the_studios_own_logging_off():
    """alembic's fileConfig disables every existing logger unless told not to.

    Both long-running processes configure logging and then migrate at startup,
    so the default would silence every `dailyfive.*` warning for the life of the
    process — the A&R's duplicate-brief warning, the Director's genre warning,
    every retry and fallback — while nothing at all appeared to fail.
    """
    import logging

    from dailyfive.db import init_db

    before = logging.getLogger("dailyfive.testcanary")
    assert not before.disabled
    init_db()
    assert not before.disabled, "a migration turned the application's loggers off"
