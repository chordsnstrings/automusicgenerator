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
             clip_id: int | None, run_id: int | None) -> str:
        from datetime import timedelta

        from .db import session_scope
        from .models import StoredFile, utcnow

        digest = hashlib.sha256(data).hexdigest()
        expires = utcnow() + timedelta(days=self.retention_days)
        kind = _kind_for(key, content_type)

        with session_scope() as s:
            row = s.query(StoredFile).filter(StoredFile.key == key).one_or_none()
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
        log.info("stored %s in database (%.1f MB, expires %s)",
                 key, len(data) / 1e6, expires.date())
        return key

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


def _kind_for(key: str, content_type: str) -> str:
    name = key.rsplit("/", 1)[-1].lower()
    if name.endswith(".wav"):
        return "wav"
    if name.endswith(".mp3"):
        return "mp3"
    if name.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "cover"
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
