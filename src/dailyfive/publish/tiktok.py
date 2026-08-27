"""TikTok Content Posting API — three steps, and one that has to be asked first.

Unlike YouTube, the privacy level is not the uploader's to choose freely. The
options a given account may use come from ``creator_info/query``, and an app
that has not passed TikTok's audit is limited to SELF_ONLY — a post nobody but
the account holder can see. Asking first and using what comes back is the
difference between a post that appears and one that is accepted and then
invisible, which looks identical from here.

The upload itself is init, PUT, poll. TikTok does not return a video id from
the init call; it returns a publish id, and the real id arrives once the
platform has finished processing — so the poll is not politeness, it is where
the identifier comes from.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx

from ..errors import ProviderError
from ..http import request
from . import Uploaded, tokens

log = logging.getLogger(__name__)

PROVIDER = "tiktok"
BASE = "https://open.tiktokapis.com/v2"

# Most permissive first. The account's own list decides; this only orders the
# candidates so a fully-audited app posts publicly and an un-audited one still
# posts rather than failing.
PRIVACY_ORDER = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS",
                 "FOLLOWER_OF_CREATOR", "SELF_ONLY")

# One chunk. TikTok accepts a whole-file upload below 64 MB, and a hook short at
# 20 seconds is a few megabytes — chunking would be machinery for a case this
# format cannot reach.
WHOLE_FILE_LIMIT = 64 * 1024 * 1024


def _refresh(refresh_token: str) -> dict:
    client_key = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
    if not client_key or not client_secret:
        raise ProviderError(PROVIDER, "TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET "
                                      "must be set to refresh a token",
                            retryable=False)
    resp = httpx.post(f"{BASE}/oauth/token/", data={
        "client_key": client_key, "client_secret": client_secret,
        "grant_type": "refresh_token", "refresh_token": refresh_token,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=60.0)
    if resp.status_code >= 400:
        raise ProviderError(PROVIDER, f"token refresh HTTP {resp.status_code}: "
                                      f"{resp.text[:300]}",
                            retryable=resp.status_code >= 500,
                            status=resp.status_code)
    body = resp.json()
    if body.get("error"):
        raise ProviderError(PROVIDER, f"token refresh: {body.get('error')} "
                                      f"{body.get('error_description', '')}",
                            retryable=False)
    return {"access_token": body.get("access_token"),
            "refresh_token": body.get("refresh_token"),
            "expires_in": body.get("expires_in"),
            "account_id": body.get("open_id"),
            "scope": body.get("scope")}


def _token() -> str:
    return tokens.current(PROVIDER, _refresh).access_token


def _check(body: dict, what: str) -> dict:
    """TikTok answers 200 with an error object, like every other v1-shaped API here."""
    err = body.get("error") or {}
    code = err.get("code")
    if code and code != "ok":
        raise ProviderError(PROVIDER, f"{what}: {code} — "
                                      f"{err.get('message', '')[:200]}",
                            retryable=str(code).startswith("rate_limit"))
    return body.get("data") or {}


def creator_info(access: str | None = None) -> dict:
    """What this account may post, and under which privacy levels."""
    access = access or _token()
    resp = request("POST", f"{BASE}/post/publish/creator_info/query/",
                   provider=PROVIDER,
                   headers={"Authorization": f"Bearer {access}",
                            "Content-Type": "application/json; charset=UTF-8"},
                   json={}, timeout=60.0)
    return _check(resp.json(), "creator_info")


def _privacy(info: dict, wanted: str) -> str:
    """The requested level if the account allows it, else the best it does allow."""
    allowed = info.get("privacy_level_options") or []
    if not allowed:
        raise ProviderError(PROVIDER, "the account reports no usable privacy "
                                      "levels — the app is probably unaudited "
                                      "and unapproved", retryable=False)
    if wanted in allowed:
        return wanted
    for level in PRIVACY_ORDER:
        if level in allowed:
            log.warning("tiktok will not accept %s for this account; posting as %s",
                        wanted, level)
            return level
    return allowed[0]


def upload(file: Path, *, title: str, description: str = "",
           tags: list[str] | None = None, privacy: str = "public",
           poll_s: float = 5.0, timeout_s: float = 600.0,
           sleep=time.sleep) -> Uploaded:
    """Post one file, wait for TikTok to finish with it, return its id."""
    access = _token()
    size = file.stat().st_size
    if size > WHOLE_FILE_LIMIT:
        raise ProviderError(PROVIDER, f"{file.name} is {size / 1e6:.0f} MB, over "
                                      f"the single-chunk limit", retryable=False)

    wanted = "PUBLIC_TO_EVERYONE" if privacy == "public" else "SELF_ONLY"
    level = _privacy(creator_info(access), wanted)

    # The caption is the title plus whatever hashtags were asked for. TikTok has
    # no separate description field — what a viewer reads is one string.
    caption = " ".join([title, *(f"#{t.lstrip('#')}" for t in (tags or []))]).strip()

    init = request("POST", f"{BASE}/post/publish/video/init/", provider=PROVIDER,
                   headers={"Authorization": f"Bearer {access}",
                            "Content-Type": "application/json; charset=UTF-8"},
                   json={
                       "post_info": {
                           "title": caption[:2200],
                           "privacy_level": level,
                           "disable_duet": False,
                           "disable_comment": False,
                           "disable_stitch": False,
                       },
                       "source_info": {
                           "source": "FILE_UPLOAD",
                           "video_size": size,
                           "chunk_size": size,
                           "total_chunk_count": 1,
                       },
                   }, timeout=120.0)
    data = _check(init.json(), "publish/video/init")
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")
    if not publish_id or not upload_url:
        raise ProviderError(PROVIDER, f"init returned no upload target: "
                                      f"{str(data)[:300]}", retryable=True)

    put = httpx.put(upload_url, content=file.read_bytes(),
                    headers={"Content-Type": "video/mp4",
                             "Content-Length": str(size),
                             "Content-Range": f"bytes 0-{size - 1}/{size}"},
                    timeout=1800.0)
    if put.status_code >= 400:
        raise ProviderError(PROVIDER, f"upload HTTP {put.status_code}: "
                                      f"{put.text[:300]}",
                            retryable=put.status_code >= 500,
                            status=put.status_code)

    video_id = _await_publish(access, publish_id, poll_s=poll_s,
                              timeout_s=timeout_s, sleep=sleep)
    return Uploaded(external_id=video_id,
                    url=f"https://www.tiktok.com/video/{video_id}",
                    extra={"privacy": level, "publish_id": publish_id,
                           "bytes": size})


def _await_publish(access: str, publish_id: str, *, poll_s: float,
                   timeout_s: float, sleep) -> str:
    """Wait for processing, because the video id does not exist until it ends."""
    deadline = time.monotonic() + timeout_s
    while True:
        resp = request("POST", f"{BASE}/post/publish/status/fetch/",
                       provider=PROVIDER,
                       headers={"Authorization": f"Bearer {access}",
                                "Content-Type": "application/json; charset=UTF-8"},
                       json={"publish_id": publish_id}, timeout=60.0)
        data = _check(resp.json(), "publish/status/fetch")
        status = data.get("status")
        if status == "PUBLISH_COMPLETE":
            ids = data.get("publicaly_available_post_id") or data.get("post_id") or []
            if isinstance(ids, list) and ids:
                return str(ids[0])
            # Complete but private: SELF_ONLY posts have no public id, and the
            # publish id is the only handle that exists. Returned rather than
            # failed, because the post is genuinely there.
            return str(publish_id)
        if status in ("FAILED", "PUBLISH_FAILED"):
            raise ProviderError(PROVIDER, f"publish failed: "
                                          f"{data.get('fail_reason', 'no reason given')}",
                                retryable=False)
        if time.monotonic() >= deadline:
            raise ProviderError(PROVIDER, f"still {status!r} after {timeout_s:.0f}s",
                                retryable=True)
        sleep(poll_s)


def statistics(video_ids: list[str]) -> dict[str, dict]:
    """View, like, comment and share counts, keyed by video id.

    Twenty ids per call, which is TikTok's page size. Unlike YouTube this one
    does report shares, and on a platform where a song spreads by being reused
    that is the number that matters most.
    """
    out: dict[str, dict] = {}
    if not video_ids:
        return out
    access = _token()

    for i in range(0, len(video_ids), 20):
        batch = video_ids[i:i + 20]
        resp = request("POST", f"{BASE}/video/query/", provider=PROVIDER,
                       headers={"Authorization": f"Bearer {access}",
                                "Content-Type": "application/json; charset=UTF-8"},
                       params={"fields": "id,view_count,like_count,"
                                         "comment_count,share_count"},
                       json={"filters": {"video_ids": batch}}, timeout=60.0)
        data = _check(resp.json(), "video/query")
        for item in data.get("videos") or []:
            out[str(item.get("id"))] = {
                "views": item.get("view_count"),
                "likes": item.get("like_count"),
                "comments": item.get("comment_count"),
                "shares": item.get("share_count"),
                "raw": item,
            }
    return out
