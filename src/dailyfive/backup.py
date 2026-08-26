"""Database backup.

The database is the learning record. The songs are replaceable — they are in
Spaces, and worst case the studio makes more tomorrow. What is not replaceable
is every clip row, every rejection reason and every rating you have given,
because that is the only thing making day 300 better than day 1 and there is
nowhere else it exists.

So this backs up the database, and it puts the copy somewhere other than the
droplet the database lives on — or, when nowhere else is configured, says so
rather than writing the copy back into the thing it is a copy of. That is not a
hypothetical: AUDIO_STORE defaults to ``database``, and reusing the pipeline's
own store here put every night's pg_dump into a row of the cluster it dumped.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .config import settings
from .errors import DailyFiveError

log = logging.getLogger(__name__)


def dump(dest_dir: Path | str | None = None) -> Path:
    """Write a consistent, compressed dump. Returns the file path."""
    cfg = settings()
    dest_dir = Path(dest_dir or (cfg.work_dir / "backups"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    url = cfg.database_url

    if url.startswith("sqlite"):
        return _dump_sqlite(url, dest_dir / f"dailyfive-{stamp}.sqlite.gz")
    if url.startswith(("postgresql", "postgres")):
        return _dump_postgres(url, dest_dir / f"dailyfive-{stamp}.sql.gz")
    raise DailyFiveError(f"don't know how to back up {url.split('://')[0]}")


def _dump_sqlite(url: str, dest: Path) -> Path:
    """Uses the backup API, not a file copy.

    A running system is mid-transaction often enough that copying the file
    yields a corrupt or half-written database, and WAL mode makes it worse —
    the copy misses whatever is still in the -wal sidecar.
    """
    path = url.split("///", 1)[-1].split("?", 1)[0]
    if not Path(path).is_file():
        raise DailyFiveError(f"no database at {path}")

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "snapshot.sqlite"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out = sqlite3.connect(staged)
            with out:
                src.backup(out)
            out.close()
        finally:
            src.close()
        _gzip(staged, dest)
    log.info("backed up sqlite -> %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest


def _dump_postgres(url: str, dest: Path) -> Path:
    if not shutil.which("pg_dump"):
        raise DailyFiveError("pg_dump not on PATH (apt-get install -y postgresql-client)")
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "dump.sql"
        # psycopg's URL scheme is not one libpq recognises.
        libpq = url.replace("postgresql+psycopg://", "postgresql://") \
                   .replace("postgresql+psycopg2://", "postgresql://")
        with staged.open("wb") as fh:
            proc = subprocess.run(["pg_dump", "--no-owner", "--no-privileges", libpq],
                                  stdout=fh, stderr=subprocess.PIPE, timeout=1800)
        if proc.returncode != 0:
            raise DailyFiveError(f"pg_dump failed: {proc.stderr.decode()[:400]}")
        _gzip(staged, dest)
    log.info("backed up postgres -> %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest


def _gzip(src: Path, dest: Path) -> None:
    with src.open("rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)


def to_storage(keep_local: int = 7) -> str | None:
    """Dump, upload off the box, and prune old local copies.

    A backup on the same droplet as the database survives a bad migration and
    nothing else, so the copy that matters is the one in Spaces.

    Deliberately NOT ``open_store()``, which is the obvious call and the wrong
    one. AUDIO_STORE defaults to ``database``, so open_store() returns a
    DatabaseStore and this wrote the pg_dump into a ``stored_files`` row inside
    the very cluster it was a dump of. Two failures at once, and neither is
    visible from the log line, which said "backup stored at …" either way.
    There is no disaster recovery — lose the cluster and every backup goes with
    it. And it compounds: the dump contains ``stored_files``, so each night's
    dump contains every retained night before it. Measured on a copy of
    production at 20 KB of audio, six consecutive nights went 5 KB, 12 KB,
    27 KB, 44 KB, 63 KB, 84 KB, and a managed Postgres that fills its disk goes
    read-only with the only copy of the delivered audio inside it.

    So the target is chosen on one question — is it somewhere else — and when
    the answer is no the honest outcome is a local copy and a loud line saying
    the off-site copy does not exist, not a key that implies one does.
    """
    from .storage import Spaces

    cfg = settings()
    path = dump()
    key = f"{cfg.spaces_prefix}/_backups/{path.name}"
    if not (cfg.spaces_bucket and cfg.spaces_key and cfg.spaces_secret):
        log.error("no off-site backup: SPACES_* is not configured, so the only "
                  "copy of %s is on this container, which is replaced on every "
                  "deploy. The database it is a dump of is not a place to put "
                  "it.", path.name)
        key = None
    else:
        try:
            Spaces().upload(path, key)
        except Exception as exc:
            log.error("backup uploaded nowhere: %s", exc)
            key = None

    # Pruning runs whichever way that went. Returning early on a missing
    # destination would leave every dump ever taken on the container's disk,
    # which is the failure this call is nominally preventing.
    backups = sorted((path.parent).glob("dailyfive-*"), reverse=True)
    for stale in backups[keep_local:]:
        stale.unlink(missing_ok=True)
        log.info("pruned local backup %s", stale.name)
    return key


def restore_hint() -> str:
    url = settings().database_url
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        return (f"gunzip -c <backup>.sqlite.gz > {path}\n"
                f"  (stop dailyfive-web first; the -wal and -shm sidecars must go too)")
    return ("gunzip -c <backup>.sql.gz | psql <DATABASE_URL>\n"
            "  (into an empty database — the dump does not drop objects)")
