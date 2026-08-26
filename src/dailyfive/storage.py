"""DigitalOcean Spaces, S3-compatible.

The single rule this module exists to enforce: once bytes are here, no
upstream expiry matters. Suno deletes generated files after 15 days and its
download URLs are short-lived; ModelArk URLs last about a week. Everything is
mirrored the moment it exists.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .config import settings
from .errors import ProviderError

log = logging.getLogger(__name__)

PROVIDER = "spaces"


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

    def upload(self, local: Path | str, key: str, *, public: bool = False) -> str:
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
                 public: bool = False) -> str:
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
