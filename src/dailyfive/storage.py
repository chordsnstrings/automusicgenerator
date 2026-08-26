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
        return self._put(local.read_bytes(), key,
                         mimetypes.guess_type(local.name)[0] or "application/octet-stream",
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
        ctype = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
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
