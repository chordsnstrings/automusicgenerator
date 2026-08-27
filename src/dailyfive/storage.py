"""DigitalOcean Spaces, S3-compatible.

The single rule this module exists to enforce: once bytes are here, no
upstream expiry matters. Suno deletes generated files after 15 days and its
download URLs are short-lived; ModelArk URLs last about a week. Everything is
mirrored the moment it exists.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import shutil
import time
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .config import settings
from .errors import ProviderError

log = logging.getLogger(__name__)

PROVIDER = "spaces"

# What an artifact is announced as must not be a property of whichever packages
# the base image happens to carry. mimetypes.guess_type() consults
# /etc/mime.types when it is present, so the same filename resolves differently
# on a developer box (which has the media-types package) and in python:3.12-slim
# (which does not), and CPython moved the built-in .wav mapping in 3.13 — a
# routine base-image bump would silently change what production serves for every
# WAV, with no code change and no test to notice. Pinning the handful of
# extensions this studio actually writes makes the answer a decision instead of
# an accident; anything unlisted still falls back to guess_type.
#
# .lrc is the one a reader will want to argue with. There is no registered IANA
# type for LRC, so there is nothing to be faithful to, and the tempting
# invention text/lrc is worse than useless: a browser displays only the text/
# subtypes it recognises and downloads the rest, so a made-up subtype reproduces
# the exact symptom being fixed and lies about the format as well. text/plain is
# the only type that both renders inline and survives the nosniff header this
# origin has to send, and it costs nothing downstream because LRC-aware players
# parse the file, never the HTTP header. The charset is not decorative — lyrics
# carry em-dashes and accented characters. It is spelled out here rather than
# left to Starlette, which appends it to any text/ media type on the way out,
# because that rescue exists only on the database path and the S3 path returns
# whatever was stored.
CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
    ".lrc": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    # Pinned for the same reason as .wav: guess_type consults /etc/mime.types,
    # which python:3.12-slim does not carry, so an unpinned .mp4 would be
    # announced as application/octet-stream in production and video/mp4 on a
    # developer box — and a browser downloads the first rather than playing it.
    ".mp4": "video/mp4",
    # A backup is dailyfive-<stamp>.sql.gz, and guess_type answers
    # ('application/sql', 'gzip') — both callers take [0] and drop the encoding,
    # so the bytes of a gzip stream would be announced as SQL text. RFC 6713's
    # application/gzip describes the container, which is the only thing one
    # content_type column can honestly say about it.
    ".gz": "application/gzip",
}


def content_type_for(name: str) -> str:
    """What to stamp into stored_files.content_type for a file of this name."""
    lowered = name.lower()
    for ext, ctype in CONTENT_TYPES.items():
        if lowered.endswith(ext):
            return ctype
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class LocalStore:
    """Filesystem stand-in for Spaces, used when no bucket is configured.

    The point is that a run should be possible before DigitalOcean is set up.
    It mirrors the Spaces interface exactly — same keys, same layout — so the
    day a bucket appears, nothing above this changes and yesterday's folders
    can be copied up untouched.

    It is not a substitute for the real thing: the whole reason Spaces exists in
    this design is that Suno deletes its own copies after 15 days, and a local
    directory on an ephemeral droplet is no safer than that.
    """

    def __init__(self, root: Path | str | None = None):
        cfg = settings()
        self.root = Path(root or (cfg.work_dir / "delivered")).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.bucket = str(self.root)
        self.prefix = cfg.spaces_prefix
        self.public_index = True

    def key_for(self, run_date: str, *parts: str) -> str:
        return "/".join([self.prefix, run_date, *[p.strip("/") for p in parts if p]])

    def _path(self, key: str) -> Path:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def upload(self, local: Path | str, key: str, *, public: bool = False,
               clip_id: int | None = None, run_id: int | None = None) -> str:
        local = Path(local)
        if not local.is_file():
            raise ProviderError("local", f"nothing to upload at {local}", retryable=False)
        shutil.copy2(local, self._path(key))
        log.info("stored %s -> %s", local.name, key)
        return key

    def put_text(self, body: str, key: str, *, content_type: str = "application/json",
                 public: bool = False, clip_id: int | None = None,
                 run_id: int | None = None) -> str:
        self._path(key).write_text(body, encoding="utf-8")
        return key

    def signed_url(self, key: str, *, expires: int = 0) -> str:
        # A file:// URL so the delivered index page still plays locally.
        return (self.root / key).resolve().as_uri()

    def exists(self, key: str) -> bool:
        return (self.root / key).is_file()

    def check_access(self) -> None:
        probe = self.root / ".write-probe"
        try:
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            raise ProviderError("local", f"cannot write to {self.root}: {exc}",
                                retryable=False) from exc


class DatabaseStore:
    """Delivered files kept as rows, with an expiry stamped on write.

    Same interface as Spaces and LocalStore, so nothing above this cares which
    is in use. Two things are genuinely different and worth knowing:

    ``signed_url`` returns an application URL rather than a storage URL, because
    the bytes are only reachable through the app. That route supports Range
    requests, without which an audio element cannot seek.

    Writes carry ``expires_at``. The retention window is therefore a fact about
    each row rather than a rule a job has to remember — the purge reads that
    column and nothing else, so a row can never outlive its window by being
    missed.
    """

    def __init__(self):
        cfg = settings()
        self.bucket = "database"
        self.prefix = cfg.spaces_prefix
        self.public_index = False
        self.retention_days = cfg.retention_days

    def key_for(self, run_date: str, *parts: str) -> str:
        return "/".join([self.prefix, run_date, *[p.strip("/") for p in parts if p]])

    # ── writes ───────────────────────────────────────────────────────────────
    def upload(self, local: Path | str, key: str, *, public: bool = False,
               clip_id: int | None = None, run_id: int | None = None) -> str:
        local = Path(local)
        if not local.is_file():
            raise ProviderError("database", f"nothing to upload at {local}",
                                retryable=False)
        return self._put(local.read_bytes(), key, content_type_for(local.name),
                         clip_id=clip_id, run_id=run_id)

    def put_text(self, body: str, key: str, *, content_type: str = "application/json",
                 public: bool = False, clip_id: int | None = None,
                 run_id: int | None = None) -> str:
        return self._put(body.encode("utf-8"), key, content_type,
                         clip_id=clip_id, run_id=run_id)

    def _put(self, data: bytes, key: str, content_type: str, *,
             clip_id: int | None, run_id: int | None,
             attempts: int = 3, sleep=time.sleep) -> str:
        """Write one file, and survive the cluster hanging up mid-statement.

        The retry is here because it happened: on 2026-08-27 a run shipped all
        five songs and then died on this INSERT with

            psycopg.OperationalError: consuming input failed:
            SSL error: unexpected eof while reading

        which is the managed cluster — one vCPU, one gigabyte — dropping the
        connection while a fifty-megabyte master streamed into it. Nothing was
        wrong with the data or the schema, and the same insert succeeds on the
        next connection.

        pool_pre_ping cannot help with this. It proves a connection is alive
        BEFORE a statement, and this one dies DURING one; the checkout was
        genuinely healthy. The only thing that recovers it is issuing the
        statement again on a new connection, which is safe here because the
        write is an upsert keyed on `key` — a retry after a partial write
        overwrites the same row rather than adding one.

        Disposing the pool between attempts matters: after an SSL-level failure
        the other pooled connections to the same backend are often dead too, and
        retrying onto one of them just fails again faster.
        """
        from datetime import timedelta

        from sqlalchemy.exc import DBAPIError, OperationalError

        from .db import engine, session_scope
        from .models import StoredFile, utcnow

        digest = hashlib.sha256(data).hexdigest()
        expires = utcnow() + timedelta(days=self.retention_days)
        kind = _kind_for(key, content_type)

        # Already here, byte for byte: refresh the window and stop. This is what
        # makes resuming a half-finished delivery cheap. A run that died partway
        # through shipping has to be re-run to finish, and without this the
        # re-run rewrites every master it already stored — two hundred megabytes
        # of binary column through a one-gigabyte cluster, which is the exact
        # load that dropped the connection the first time. The repair would
        # reliably reproduce the fault it exists to repair.
        if self._unchanged(key, digest, len(data)):
            self._touch(key, expires)
            log.info("%s is already stored unchanged — kept, expiry refreshed", key)
            return key

        for attempt in range(1, attempts + 1):
            try:
                with session_scope() as s:
                    row = s.query(StoredFile).filter(
                        StoredFile.key == key).one_or_none()
                    if row is None:
                        row = StoredFile(key=key)
                        s.add(row)
                    row.data = data
                    row.content_type = content_type
                    row.size_bytes = len(data)
                    row.sha256 = digest
                    row.kind = kind
                    row.expires_at = expires
                    if clip_id is not None:
                        row.clip_id = clip_id
                    if run_id is not None:
                        row.run_id = run_id
                break
            except (OperationalError, DBAPIError) as exc:
                if attempt == attempts or not _connection_lost(exc):
                    raise
                log.warning("storing %s: connection lost (attempt %d/%d) — %s",
                            key, attempt, attempts, str(exc)[:160])
                try:
                    engine().dispose()
                except Exception:            # nothing to do but try anyway
                    pass
                sleep(2.0 * attempt)

        log.info("stored %s in database (%.1f MB, expires %s)",
                 key, len(data) / 1e6, expires.date())
        return key

    def _unchanged(self, key: str, digest: str, size: int) -> bool:
        """Whether the stored copy is this exact file.

        Compares the hash AND the length. The hash alone would be enough in any
        realistic sense, but the two columns are written together and a row
        where they disagree is a row that was written by something other than
        this method — which is a reason to rewrite it, not to trust it.

        Selected by column, never as an ORM row: loading StoredFile here would
        pull the whole blob back over the wire to decide whether to send it
        again.
        """
        from sqlalchemy import select

        from .db import session_scope
        from .models import StoredFile
        try:
            with session_scope() as s:
                got = s.execute(select(StoredFile.sha256, StoredFile.size_bytes)
                                .where(StoredFile.key == key)).first()
        except Exception as exc:
            log.debug("could not check %s before writing: %s", key, exc)
            return False
        return bool(got) and got[0] == digest and got[1] == size

    def _touch(self, key: str, expires) -> None:
        """Push the retention window out without rewriting the bytes."""
        from sqlalchemy import update

        from .db import session_scope
        from .models import StoredFile
        with session_scope() as s:
            s.execute(update(StoredFile).where(StoredFile.key == key)
                      .values(expires_at=expires))

    # ── reads ────────────────────────────────────────────────────────────────
    def signed_url(self, key: str, *, expires: int = 0) -> str:
        base = settings().public_base_url or ""
        return f"{base}/files/{key}"

    def fetch(self, key: str) -> tuple[bytes, str] | None:
        from .db import session_scope
        from .models import StoredFile
        with session_scope() as s:
            row = s.query(StoredFile).filter(StoredFile.key == key).one_or_none()
            if row is None:
                return None
            return row.data, row.content_type

    def exists(self, key: str) -> bool:
        from .db import session_scope
        from .models import StoredFile
        with session_scope() as s:
            return s.query(StoredFile.id).filter(StoredFile.key == key).first() is not None

    def check_access(self) -> None:
        from .db import session_scope
        from .models import StoredFile
        try:
            with session_scope() as s:
                s.query(StoredFile.id).limit(1).all()
        except Exception as exc:
            raise ProviderError("database", f"stored_files unreachable: {exc}",
                                retryable=False) from exc


def retype_stored_files(dry_run: bool = False) -> dict:
    """Bring rows already in the table into line with CONTENT_TYPES.

    The content type is stamped at write time, so fixing the derivation does
    nothing for the rows that are already wrong — and there is no read hook on
    the Spaces path at all, which is the argument against recomputing this on
    the way out. Recomputing on read would also leave the column permanently
    wrong while it is still what the S3 mirror sends, what a restore carries and
    what _kind_for consults, which is two sources of truth drifting apart.

    Deliberately a re-runnable command rather than an Alembic data migration. A
    migration fires once, guarded by alembic_version, and there is no way to
    make it fire again without hand-editing that table — so a row written wrong
    by an old container mid rolling deploy, or restored from a backup taken
    before the fix, stays wrong forever. This is idempotent by construction: on
    a clean table the comparison matches nothing and it issues no UPDATE.

    Columns are selected by name so `data` is never loaded; a table of masters
    is tens of megabytes a row and this walks all of them.
    """
    from sqlalchemy import select, update

    from .db import session_scope
    from .models import StoredFile

    checked, fixed = 0, {}
    with session_scope() as s:
        rows = s.execute(select(StoredFile.id, StoredFile.key,
                                StoredFile.content_type)).all()
        for row_id, key, stored in rows:
            checked += 1
            correct = content_type_for(key.rsplit("/", 1)[-1])
            if stored == correct:
                continue
            fixed[correct] = fixed.get(correct, 0) + 1
            if not dry_run:
                s.execute(update(StoredFile)
                          .where(StoredFile.id == row_id)
                          .values(content_type=correct))

    total = sum(fixed.values())
    if total:
        log.info("retype: %d of %d stored files %s (%s)", total, checked,
                 "would be corrected" if dry_run else "corrected",
                 ", ".join(f"{n} -> {t}" for t, n in sorted(fixed.items())))
    return {"checked": checked, "fixed": total, "by_type": fixed}


def remeta_stored_files(dry_run: bool = False) -> dict:
    """Stop a stored meta.json naming a cover that was never made.

    The same argument as retype_stored_files, applied to the other thing this
    system stamps at write time. Every manifest shipped before cover art became
    conditional says ``"cover": "cover.jpg"``, and not one of those files has
    ever existed; correcting build_meta corrects tomorrow's, while the days
    already in the table go on making a machine-readable false statement for
    the rest of their retention window to anything that parses them.

    Only the cover entry, and only when the sibling object is genuinely absent.
    The other four names are written unconditionally by the packager, so a
    missing master.wav means the purge has been through — a window expiring is
    not a manifest to correct, and blanking those would turn retention into
    data loss.

    Rewritten in place rather than through put_text, which stamps a fresh
    expires_at: repairing a sentence about a day must not extend how long that
    day's bytes are kept.

    Like retype, this reaches the database store only. There is no read hook on
    the Spaces path and no way to walk it from here.
    """
    import json

    from sqlalchemy import select, update

    from .db import session_scope
    from .models import StoredFile

    checked = fixed = 0
    with session_scope() as s:
        present = {k for (k,) in s.execute(select(StoredFile.key)).all()}
        rows = s.execute(
            select(StoredFile.id, StoredFile.key, StoredFile.data)
            .where(StoredFile.key.like("%/meta.json"))).all()
        for row_id, key, data in rows:
            checked += 1
            try:
                doc = json.loads(bytes(data).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                # Not this job's problem to diagnose, and certainly not its job
                # to overwrite something it could not read.
                log.warning("remeta: %s is not readable JSON, left alone", key)
                continue
            files = doc.get("files") if isinstance(doc, dict) else None
            cover = files.get("cover") if isinstance(files, dict) else None
            if not cover:
                continue
            if key.rsplit("/", 1)[0] + "/" + str(cover) in present:
                continue
            fixed += 1
            if dry_run:
                continue
            files["cover"] = None
            body = json.dumps(doc, indent=2).encode("utf-8")
            s.execute(update(StoredFile).where(StoredFile.id == row_id)
                      .values(data=body, size_bytes=len(body),
                              sha256=hashlib.sha256(body).hexdigest()))

    if fixed:
        log.info("remeta: %d of %d manifests %s", fixed, checked,
                 "would drop a cover that was never made" if dry_run
                 else "no longer name a cover that was never made")
    return {"checked": checked, "fixed": fixed}


# What a mid-statement disconnect looks like across the drivers this runs on.
# Matched on text because psycopg raises OperationalError for both a dropped
# connection and a dozen unrelated conditions, and retrying a genuine
# constraint violation three times is three identical failures and a slower
# error message.
_LOST = ("ssl error", "unexpected eof", "server closed the connection",
         "connection already closed", "consuming input failed",
         "terminating connection", "connection reset")


def _connection_lost(exc: Exception) -> bool:
    if getattr(exc, "connection_invalidated", False):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _LOST)


def _kind_for(key: str, content_type: str) -> str:
    name = key.rsplit("/", 1)[-1].lower()
    if name.endswith(".wav"):
        return "wav"
    if name.endswith(".mp3"):
        return "mp3"
    if name.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "cover"
    # Its own kind, not "other". The purge, the retype pass and the storage
    # summary all group by this column, and a 6 MB video filed under the same
    # label as a stray text file is a number nobody can act on.
    if name.endswith((".mp4", ".mov", ".webm")):
        return "video"
    if content_type.startswith("text/") or name.endswith((".json", ".txt", ".lrc", ".html")):
        return "text"
    return "other"


def open_store():
    """Spaces when it is configured, the filesystem when it is not.

    Chosen once per run so the whole run agrees about where things went.
    """
    cfg = settings()
    choice = cfg.audio_store

    if choice == "database":
        return DatabaseStore()
    if choice == "spaces" or (choice == "auto" and cfg.spaces_bucket):
        if cfg.spaces_bucket and cfg.spaces_key and cfg.spaces_secret:
            return Spaces()
        log.error("AUDIO_STORE=spaces but SPACES_* is incomplete — falling back "
                  "to the filesystem, which is not a place to keep a catalogue")
        return LocalStore()
    if choice == "local":
        return LocalStore()

    if cfg.spaces_bucket and cfg.spaces_key and cfg.spaces_secret:
        return Spaces()
    log.warning("no delivery target configured — writing to %s. Suno deletes "
                "its own copies after 15 days, so this is not somewhere to keep "
                "a catalogue.", cfg.work_dir / "delivered")
    return LocalStore()


class Spaces:
    def __init__(self):
        cfg = settings()
        cfg.require("spaces_key", "spaces_secret", "spaces_bucket", "spaces_endpoint")
        self.bucket = cfg.spaces_bucket
        self.prefix = cfg.spaces_prefix
        self.public_index = cfg.spaces_public_index
        self._client = boto3.client(
            "s3",
            region_name=cfg.spaces_region,
            endpoint_url=cfg.spaces_endpoint,
            aws_access_key_id=cfg.spaces_key,
            aws_secret_access_key=cfg.spaces_secret,
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 5,
                                                                "mode": "standard"}),
        )

    def key_for(self, run_date: str, *parts: str) -> str:
        return "/".join([self.prefix, run_date, *[p.strip("/") for p in parts if p]])

    def upload(self, local: Path | str, key: str, *, public: bool = False,
               clip_id: int | None = None, run_id: int | None = None) -> str:
        local = Path(local)
        if not local.is_file():
            raise ProviderError(PROVIDER, f"nothing to upload at {local}", retryable=False)
        ctype = content_type_for(local.name)
        extra = {"ContentType": ctype}
        if public:
            extra["ACL"] = "public-read"
        try:
            self._client.upload_file(str(local), self.bucket, key, ExtraArgs=extra)
        except (BotoCoreError, ClientError) as exc:
            raise ProviderError(PROVIDER, f"upload {key} failed: {exc}", retryable=True) from exc
        log.info("uploaded %s -> s3://%s/%s (%.1f MB)", local.name, self.bucket, key,
                 local.stat().st_size / 1e6)
        return key

    def put_text(self, body: str, key: str, *, content_type: str = "application/json",
                 public: bool = False, clip_id: int | None = None,
                 run_id: int | None = None) -> str:
        extra = {"ContentType": content_type}
        if public:
            extra["ACL"] = "public-read"
        try:
            self._client.put_object(Bucket=self.bucket, Key=key,
                                    Body=body.encode("utf-8"), **extra)
        except (BotoCoreError, ClientError) as exc:
            raise ProviderError(PROVIDER, f"put {key} failed: {exc}", retryable=True) from exc
        return key

    def signed_url(self, key: str, *, expires: int = 7 * 24 * 3600) -> str:
        try:
            return self._client.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires)
        except (BotoCoreError, ClientError) as exc:
            raise ProviderError(PROVIDER, f"signing {key} failed: {exc}", retryable=True) from exc

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def check_access(self) -> None:
        """Fail at startup rather than three hours in, after the credits are spent."""
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError) as exc:
            raise ProviderError(PROVIDER,
                                f"cannot reach bucket {self.bucket!r}: {exc}",
                                retryable=False) from exc


def strip_wav_metadata(dry_run: bool = False, limit: int = 0,
                       pause_s: float = 4.0) -> dict:
    """Remove the INFO chunk from WAV rows written before the master stopped
    carrying one.

    Suno stamps every WAV it renders with a comment naming itself, the render
    timestamp and its internal clip id, and ffmpeg copied that chunk forward
    until WAV_NO_METADATA was added to the mastering command. Every file
    delivered before that announces where it came from, which is the one thing a
    delivery master should not do.

    This rewrites `data`, which is the only copy of the audio this system holds
    — Suno deletes its own after fifteen days and there is no bucket behind
    this. So the row is only updated when ffmpeg's own hash of the decoded audio
    stream is byte-identical before and after. A container rewrite that changed
    a single sample would fail that comparison and be discarded rather than
    stored; the operation is a chunk edit, and this proves it stayed one.

    One file at a time, through a temporary file rather than memory, and columns
    selected by name so no other row's `data` is ever loaded.

    It also waits between files, which is not politeness. The first attempt at
    this walked five masters back to back — read 57 MB, rewrite 57 MB, repeat —
    and the managed cluster is one vCPU with a gigabyte of RAM. It stopped
    accepting connections partway through, the web service answered 500 for a
    minute, and the worker only recovered because startup retries with backoff.
    No row was damaged, because each update is its own transaction and is only
    issued once the audio hash matches. But a repair that walks large binary
    columns has to leave the cluster room to serve the application it belongs
    to, and a few seconds a file is cheaper than an outage.
    """
    import subprocess
    import tempfile
    import time

    from sqlalchemy import select, update

    from .db import session_scope
    from .models import StoredFile

    def audio_hash(path: Path) -> str | None:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                            "-map", "0:a", "-f", "md5", "-"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    with session_scope() as s:
        rows = s.execute(select(StoredFile.id, StoredFile.key)
                         .where(StoredFile.kind == "wav")
                         .order_by(StoredFile.id)).all()
    if limit:
        rows = rows[:limit]

    out = {"checked": 0, "stripped": 0, "already_clean": 0,
           "skipped": [], "dry_run": dry_run}

    for n, (row_id, key) in enumerate(rows):
        if n and pause_s:
            time.sleep(pause_s)
        out["checked"] += 1
        with session_scope() as s:
            blob = s.execute(select(StoredFile.data)
                             .where(StoredFile.id == row_id)).scalar_one_or_none()
        if not blob:
            out["skipped"].append({"key": key, "why": "no bytes"})
            continue

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.wav"
            dst = Path(tmp) / "out.wav"
            src.write_bytes(blob)

            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format_tags",
                 "-of", "default=nw=1", str(src)], capture_output=True, text=True)
            if not probe.stdout.strip():
                out["already_clean"] += 1
                continue

            before = audio_hash(src)
            # -c copy: the samples are not re-encoded, only the container is
            # rewritten without its INFO chunk.
            enc = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c", "copy",
                 "-map_metadata", "-1", "-bitexact", str(dst)],
                capture_output=True, text=True)
            if enc.returncode != 0 or not dst.is_file():
                out["skipped"].append({"key": key, "why": f"ffmpeg: {enc.stderr[:120]}"})
                continue

            after = audio_hash(dst)
            if not before or before != after:
                out["skipped"].append({"key": key, "why": "audio hash changed — not stored"})
                continue

            cleaned = dst.read_bytes()
            if not dry_run:
                with session_scope() as s:
                    s.execute(update(StoredFile).where(StoredFile.id == row_id).values(
                        data=cleaned, size_bytes=len(cleaned),
                        sha256=hashlib.sha256(cleaned).hexdigest()))
            out["stripped"] += 1

    return out
