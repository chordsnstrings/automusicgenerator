"""YouTube Data API v3 — upload a video, then read what it did.

Resumable upload rather than a single multipart POST, and not for the reason
the name suggests. A multipart upload of a 40 MB file has to be held in memory
and re-sent whole on any failure; the resumable protocol asks for a session URL
first, so the metadata is accepted (and rejected, if it is going to be) before
a byte of video moves. A title the API will not take costs one small request
instead of the whole upload.

The disclosure field is the one decision in this file worth reading. YouTube
requires ``selfDeclaredMadeForKids`` on every upload — that one has no default
and an upload without it is refused — and separately asks uploaders to declare
altered or synthetic content that depicts a realistic person. These shorts are
exactly that: a photorealistic person who does not exist. The declaration is a
parameter here rather than a hidden constant, defaulting to declaring, because
the account is the thing at risk and it is not this module's call to make
quietly in either direction.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

from ..errors import ProviderError
from ..http import request
from . import Uploaded, tokens

log = logging.getLogger(__name__)

PROVIDER = "youtube"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3/videos"

# 10 is "Music". Numeric because the API takes an id, not a name, and the list
# is regional — asking for it per upload would be a call per video to learn a
# constant.
MUSIC_CATEGORY = "10"


def _refresh(refresh_token: str) -> dict:
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ProviderError(PROVIDER, "YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET "
                                      "must be set to refresh a token",
                            retryable=False)
    resp = httpx.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }, timeout=60.0)
    if resp.status_code >= 400:
        raise ProviderError(PROVIDER, f"token refresh HTTP {resp.status_code}: "
                                      f"{resp.text[:300]}",
                            retryable=resp.status_code >= 500,
                            status=resp.status_code)
    return resp.json()


def _token() -> str:
    return tokens.current(PROVIDER, _refresh).access_token


def upload(file: Path, *, title: str, description: str = "",
           tags: list[str] | None = None, privacy: str = "public",
           made_for_kids: bool = False,
           declare_synthetic: bool = True) -> Uploaded:
    """Upload one file and return its video id and watch URL."""
    access = _token()
    size = file.stat().st_size

    body = {
        "snippet": {
            # 100 characters is the hard limit and the API rejects rather than
            # truncates, so a long title fails the upload after the file has
            # moved. Trimmed here, where it costs nothing.
            "title": title[:100],
            "description": description[:5000],
            "tags": (tags or [])[:20],
            "categoryId": MUSIC_CATEGORY,
        },
        "status": {
            "privacyStatus": privacy,
            # No default exists for this one; an upload without it is refused.
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }
    if declare_synthetic:
        body["status"]["containsSyntheticMedia"] = True

    start = httpx.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={"Authorization": f"Bearer {access}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Length": str(size),
                 "X-Upload-Content-Type": "video/mp4"},
        content=json.dumps(body), timeout=60.0)
    if start.status_code >= 400:
        raise ProviderError(PROVIDER, f"upload session HTTP {start.status_code}: "
                                      f"{start.text[:400]}",
                            retryable=start.status_code >= 500,
                            status=start.status_code)
    session_url = start.headers.get("location") or start.headers.get("Location")
    if not session_url:
        raise ProviderError(PROVIDER, "no upload session URL in the response",
                            retryable=True)

    with file.open("rb") as fh:
        put = httpx.put(session_url, content=fh,
                        headers={"Content-Length": str(size),
                                 "Content-Type": "video/mp4"},
                        timeout=1800.0)
    if put.status_code >= 400:
        raise ProviderError(PROVIDER, f"upload HTTP {put.status_code}: "
                                      f"{put.text[:400]}",
                            retryable=put.status_code >= 500,
                            status=put.status_code)

    data = put.json()
    video_id = data.get("id")
    if not video_id:
        raise ProviderError(PROVIDER, f"no video id in response: {str(data)[:300]}",
                            retryable=False)
    return Uploaded(external_id=video_id,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    extra={"privacy": privacy, "bytes": size,
                           "synthetic_declared": declare_synthetic})


def statistics(video_ids: list[str]) -> dict[str, dict]:
    """View, like and comment counts, keyed by video id.

    Fifty ids per call is the API's own page size, so the batching is theirs
    rather than a guess. Shares are absent from the YouTube statistics resource
    — it does not expose one — and the key is left out rather than reported as
    zero, which would read as "nobody shared it".
    """
    out: dict[str, dict] = {}
    if not video_ids:
        return out
    access = _token()

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = request("GET", API_URL, provider=PROVIDER,
                       headers={"Authorization": f"Bearer {access}"},
                       params={"part": "statistics", "id": ",".join(batch)},
                       timeout=60.0)
        if resp.status_code >= 400:
            raise ProviderError(PROVIDER, f"statistics HTTP {resp.status_code}: "
                                          f"{resp.text[:300]}",
                                retryable=resp.status_code >= 500,
                                status=resp.status_code)
        for item in resp.json().get("items") or []:
            st = item.get("statistics") or {}
            out[item.get("id")] = {
                "views": _int(st.get("viewCount")),
                "likes": _int(st.get("likeCount")),
                "comments": _int(st.get("commentCount")),
                "raw": st,
            }
    return out


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
